"""Frozen Git input identities for platform-smoke-proof/v1.

Git is read only. Missing ancillary identities disable reuse; a contradictory
immutable request is an integrity error. Candidate attribution is deliberately
outside the reusable content key.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from quality_gates.focused_smoke_selector import REGISTER_RELPATH
from quality_gates.gate_smoke_register import is_fragment_relpath

SCHEMA = "platform-smoke-proof/v1"
RULE_VERSION = "platform-focused-union/v2"
REPOSITORY_SCAN = "@repository-scan"
_ROUTING = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}


def canonical(value: object) -> bytes:
    """Canonical UTF-8 JSON, without an implicit newline."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    """SHA-256 of a canonical structured value."""
    return hashlib.sha256(canonical(value)).hexdigest()


def safe_path(value: str) -> str:
    """Require one literal relative path; never permit shell/path traversal."""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in ("..", ".git") for part in path.parts):
        raise ValueError(f"unsafe repository path: {value!r}")
    if str(path) != value or any(char in value for char in "\x00\n\r\\"):
        raise ValueError(f"noncanonical repository path: {value!r}")
    return value


def git(root: Path, *arguments: str, data: bytes | None = None) -> bytes:
    """A checked, routing-scrubbed, shell-free read-only Git call."""
    env = {key: value for key, value in os.environ.items() if key not in _ROUTING}
    result = subprocess.run(("git", "-C", str(root), *arguments), input=data, capture_output=True, env=env, check=False)
    if result.returncode:
        raise ValueError(f"Git measurement failed ({arguments!r}): {result.stderr.decode(errors='replace')}")
    return result.stdout


@dataclass(frozen=True)
class FrozenTree:
    """Immutable commit, tree and literal path/mode/blob inventory."""

    commit: str
    tree: str
    entries: dict[str, dict[str, str]]

    @property
    def manifest_sha256(self) -> str:
        return digest(self.entries)


def read_tree(root: Path, commit: str) -> FrozenTree:
    """Resolve a commit once and census its complete tree, including modes."""
    resolved = git(root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    tree = git(root, "rev-parse", f"{resolved}^{{tree}}").decode().strip()
    entries: dict[str, dict[str, str]] = {}
    for row in git(root, "ls-tree", "-rz", "--full-tree", resolved).split(b"\0"):
        if row:
            header, name = row.split(b"\t", 1)
            mode, kind, blob = header.decode().split()
            path = safe_path(name.decode("utf-8"))
            entries[path] = {"mode": mode, "kind": kind, "blob": blob}
    return FrozenTree(resolved, tree, entries)


def read_sources(root: Path, tree: FrozenTree) -> dict[str, bytes]:
    """Batch-read Python and the membership register from immutable objects."""
    names = sorted(path for path, row in tree.entries.items() if _source_input(path, row))
    blobs = [tree.entries[path]["blob"] for path in names]
    output = git(root, "cat-file", "--batch", data="".join(blob + "\n" for blob in blobs).encode())
    offset = 0
    sources: dict[str, bytes] = {}
    for path, blob in zip(names, blobs, strict=True):
        end = output.index(b"\n", offset)
        observed, kind, size = output[offset:end].decode().split()
        if observed != blob or kind != "blob":
            raise ValueError(f"wrong immutable object for {path}")
        offset = end + 1
        count = int(size)
        sources[path] = output[offset : offset + count]
        offset += count + 1
    if offset != len(output):
        raise ValueError("unexpected cat-file batch suffix")
    return sources


def _source_input(path: str, row: Mapping[str, str]) -> bool:
    """Whether a frozen-tree row is a selection input whose bytes must be read.

    The gate-smoke register is an input because the selection is computed from
    it, and so is every fragment under ``gate_smokes.d/``: those carry register
    entries on exactly the same terms (prb_ae6d81ea). Admitting the register
    without its fragments would hand ``smoke_proof`` a sources map in which the
    fragment-registered smokes simply do not exist -- a silent allowlist hole
    rather than a visible failure.
    """
    if row["mode"] not in ("100644", "100755"):
        return False
    return path.endswith(".py") or path == REGISTER_RELPATH or is_fragment_relpath(REGISTER_RELPATH, path)


def delta_entries(root: Path, base: FrozenTree, candidate: FrozenTree) -> list[dict[str, Any]]:
    """Complete no-renames delta; deletion and rename sides stay explicit."""
    words = git(root, "diff", "--no-ext-diff", "--name-status", "-z", "--no-renames", base.commit, candidate.commit, "--").split(b"\0")
    entries: list[dict[str, Any]] = []
    for index in range(0, len(words) - 1, 2):
        status, path = words[index].decode(), safe_path(words[index + 1].decode())
        entries.append({"path": path, "status": status, "base": base.entries.get(path), "candidate": candidate.entries.get(path)})
    return sorted(entries, key=lambda row: row["path"])


def freeze_request(root: Path, base_commit: str, candidate_commit: str, *, repository_id: str, named_smokes: Sequence[str] = (), attribution: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Positive request producer for the CLI, native service and PS adapter."""
    base, candidate = read_tree(root, base_commit), read_tree(root, candidate_commit)
    delta = delta_entries(root, base, candidate)
    return {
        "schema": SCHEMA,
        "repository_id": repository_id,
        "base_commit": base.commit,
        "base_tree": base.tree,
        "candidate_commit": candidate.commit,
        "candidate_tree": candidate.tree,
        "manifest_sha256": candidate.manifest_sha256,
        "delta_entries": delta,
        "delta_sha256": digest(delta),
        "named_smokes": sorted({safe_path(path) for path in named_smokes}),
        "attribution": dict(attribution or {}),
        "known_failures": {},
    }


def validate_request(root: Path, request: Mapping[str, Any]) -> tuple[FrozenTree, FrozenTree]:
    """Reject contradictory immutable identity; no caller can narrow the delta."""
    if request.get("schema") != SCHEMA or not request.get("repository_id"):
        raise ValueError("unsupported smoke proof schema or missing repository identity")
    base = read_tree(root, str(request["base_commit"]))
    candidate = read_tree(root, str(request["candidate_commit"]))
    observed: dict[str, Any] = {"base_commit": base.commit, "base_tree": base.tree, "candidate_commit": candidate.commit, "candidate_tree": candidate.tree, "manifest_sha256": candidate.manifest_sha256}
    delta = delta_entries(root, base, candidate)
    observed.update(delta_entries=delta, delta_sha256=digest(delta))
    for key, value in observed.items():
        if request.get(key) != value:
            raise ValueError(f"frozen identity mismatch: {key}")
    return base, candidate


def tool_profile(root: Path, tree: FrozenTree) -> dict[str, Any]:
    """Effective interpreter, installed dependencies, routing and runner policy."""
    from quality_gates.run_smokes import _CHILD_ENVIRONMENT_KEYS, _import_roots

    dependencies = sorted((dist.metadata.get("Name", "unknown"), dist.version) for dist in importlib.metadata.distributions())
    inputs = {path: row for path, row in tree.entries.items() if path.startswith("quality_gates/") and not path.startswith("quality_gates/tests/") or Path(path).name in ("pyproject.toml", "uv.lock", "requirements.txt", "poetry.lock")}
    paths = [str(path.resolve()).replace(str(root.resolve()), "<repository>") for path in _import_roots(root)]
    environment = {key: digest(os.environ.get(key)) for key in _CHILD_ENVIRONMENT_KEYS if key not in ("TMPDIR", "TMP", "TEMP")}
    return {
        "rule_version": RULE_VERSION,
        "python": sys.version,
        "executable": str(Path(sys.executable).resolve()),
        "platform": platform.platform(),
        "dependencies": dependencies,
        "import_paths": paths,
        "child_environment_sha256": digest(environment),
        "inputs": inputs,
        "solet_name": os.environ.get("SOLET_NAME"),
        "external_fixture_identity": os.environ.get("PLATFORM_SMOKE_FIXTURE_IDENTITY"),
    }


def verify_worktree(root: Path, tree: FrozenTree, paths: Sequence[str]) -> None:
    """Revalidate executing input bytes and modes before use/publication."""
    for relative in paths:
        safe_path(relative)
        row = tree.entries[relative]
        path = root / relative
        if path.is_symlink():
            body = os.readlink(path).encode()
            mode = "120000"
        elif not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"unsafe executing input: {relative}")
        else:
            mode = "100755" if path.stat().st_mode & 0o111 else "100644"
            body = path.read_bytes()
        blob = hashlib.sha1(f"blob {len(body)}\0".encode() + body, usedforsecurity=False).hexdigest()
        if row["mode"] != mode or row["blob"] != blob:
            raise ValueError(f"executing input differs from frozen candidate: {relative}")


def _dynamic_names(tree: ast.Module) -> tuple[set[str], list[str]]:
    names: set[str] = set()
    unknown: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            if name in ("__import__", "import_module", "spec_from_file_location", "run_path"):
                value = node.args[0] if node.args else None
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    names.add(value.value)
                else:
                    unknown.append(f"dynamic {name} at line {node.lineno}")
    return names, unknown


def dependency_graph(sources: Mapping[str, bytes], entries: Mapping[str, Any]) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """Use the selector's relative/sibling resolver, adding dynamic/resources."""
    from quality_gates.focused_smoke_selector import _ImportIndex

    index = _ImportIndex()
    graph: dict[str, set[str]] = {}
    unknown: dict[str, list[str]] = {}
    resources: dict[str, set[str]] = defaultdict(set)
    for path in entries:
        resources[PurePosixPath(path).name].add(path)
    for path, body in sources.items():
        if path.endswith(".py"):
            _index_source(path, body, entries, resources, index, graph, unknown)
    for dependency in sources:
        for importer in index.importers_of(dependency):
            graph[importer].add(dependency)
    return graph, unknown


def _index_source(path: str, body: bytes, entries: Mapping[str, Any], resources: Mapping[str, set[str]], index: Any, graph: dict[str, set[str]], unknown: dict[str, list[str]]) -> None:
    from quality_gates.focused_smoke_selector import _imported_names, _module_names

    graph[path] = set()
    try:
        tree = ast.parse(body, filename=path)
    except (SyntaxError, ValueError) as error:
        unknown[path] = [f"unparsed input: {error}"]
        return
    absolute, bare = _imported_names(tree, _module_names(path), path.endswith("/__init__.py"))
    dynamic, notices = _dynamic_names(tree)
    index.add(path, absolute | dynamic, bare | dynamic)
    notices.extend(_external_notices(tree))
    if notices:
        unknown[path] = notices
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    for literal in literals:
        matches = {literal, str(PurePosixPath(path).parent / literal)} & entries.keys()
        graph[path].update((matches | resources.get(PurePosixPath(literal).name, set())) - {path})
    if any(token in body for token in (b".rglob(", b"os.walk(", b".glob(", b"scan_repository(", b"evaluate_repository(")):
        graph[path].add(REPOSITORY_SCAN)


def _external_notices(tree: ast.Module) -> list[str]:
    """Ancillary state cannot be inferred from repository bytes alone."""
    notices: set[str] = set()
    for node in ast.walk(tree):
        if _external_import(node):
            notices.add("external process/network/database input is unmeasured; execute uncached")
        if _file_access(node):
            notices.add("file/resource target is not fully proven by literal references; execute uncached")
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "home", "gettempdir"):
            notices.add("environment/host resource input is unmeasured; execute uncached")
    return sorted(notices)


def _external_import(node: ast.AST) -> bool:
    external = {"subprocess", "socket", "requests", "httpx", "urllib", "psycopg", "asyncpg"}
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        names = [node.module or ""]
    else:
        return False
    return any(name.split(".")[0] in external for name in names)


def _file_access(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
    return name in {"open", "read_text", "read_bytes", "read_binary", "iterdir", "listdir", "stat", "lstat", "is_file", "exists", "eval", "exec", "run_module"}


def closure(graph: Mapping[str, set[str]], smoke: str) -> list[str]:
    """Full transitive input closure, with a visited set for import cycles."""
    visited: set[str] = set()
    pending = [smoke]
    while pending:
        path = pending.pop()
        if path not in visited:
            visited.add(path)
            pending.extend(graph.get(path, set()) - visited)
    return sorted(visited)
