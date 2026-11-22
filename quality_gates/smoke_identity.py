"""Frozen Git input identities for platform-smoke-proof/v1.

Git is read only. An input a smoke's closure cannot list widens that smoke's key
to the whole tree; a contradictory immutable request is an integrity error.
Candidate attribution and the repository id are deliberately outside the key.
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
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from quality_gates import private_test_pg
from quality_gates.focused_smoke_selector import REGISTER_RELPATH
from quality_gates.gate_smoke_register import is_fragment_relpath, parse_register_text

SCHEMA = "platform-smoke-proof/v1"
RULE_VERSION = "platform-focused-union/v3"
REPOSITORY_SCAN = "@repository-scan"
NEVER_REUSE_RELPATH = "quality_gates/smoke_never_reuse.txt"
RECORDED_SCOPE = "recorded"
# The recorder runs as a worker launched by path, so no import reaches it: these are tool inputs on their own,
# and a change to any of them moves every key by moving the profile.
_RECORDER_INPUTS = ("quality_gates/smoke_read_hooks.py", "quality_gates/smoke_read_worker.py", "quality_gates/smoke_reads.py")
_ROUTING = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}


def canonical(value: object) -> bytes:
    """Canonical UTF-8 JSON, without an implicit newline."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    """SHA-256 of a canonical structured value."""
    return hashlib.sha256(canonical(value)).hexdigest()


def recorded_key(smoke: str, certificate: str, profile: Mapping[str, Any]) -> str:
    """The key of a smoke whose recorded inputs hold: its certificate's digest and the tool profile.

    The certificate digest covers every observation, the resolved root's digest and the recorder's rule;
    the candidate commit and tree, the attempt and the root's name are not in it.
    """
    return digest({"schema": SCHEMA, "smoke": smoke, "scope": RECORDED_SCOPE, "certificate": certificate, "profile": profile})


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

    The never-reuse list is an input because a smoke's key depends on it. The
    gate-smoke register is an input because the selection is computed from
    it, and so is every fragment under ``gate_smokes.d/``: those carry register
    entries on exactly the same terms (prb_ae6d81ea). Admitting the register
    without its fragments would hand ``smoke_proof`` a sources map in which the
    fragment-registered smokes simply do not exist -- a silent allowlist hole
    rather than a visible failure.
    """
    if row["mode"] not in ("100644", "100755"):
        return False
    return path.endswith(".py") or path in (REGISTER_RELPATH, NEVER_REUSE_RELPATH) or is_fragment_relpath(REGISTER_RELPATH, path)


def parse_never_reuse(body: bytes | None) -> dict[str, str]:
    """The smokes that are never reused, each with its reason.

    Register grammar (``path  # reason``) read by the register's own parser, so
    there is one grammar. A path with no reason is refused: a list nobody can
    justify is not a list.
    """
    if body is None:
        return {}
    never: dict[str, str] = {}
    for line in body.decode("utf-8").splitlines():
        paths = parse_register_text(line)
        if not paths:
            continue
        reason = line.partition("#")[2].strip()
        if not reason:
            raise ValueError(f"never-reuse entry without a reason: {paths[0]}")
        never.setdefault(paths[0], reason)
    return never


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


_TOOL_SEEDS = ("quality_gates/run_smokes.py", "quality_gates/smoke_proof.py")


def _imported_modules(node: ast.AST) -> list[str]:
    """Dotted names one import statement can bring in: the module and, for ``from``, each imported name under it."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        return ([base] if base else []) + [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
    return []


def _sibling_imports(path: str, body: bytes, sources: Mapping[str, bytes]) -> set[str]:
    try:
        tree = ast.parse(body, filename=path)
    except (SyntaxError, ValueError):
        return set()  # an unparsed tool file is still an input; the run itself fails on it
    siblings = {f"quality_gates/{name.removeprefix('quality_gates.').split('.')[0]}.py" for node in ast.walk(tree) for name in _imported_modules(node)}
    return siblings & sources.keys()


def tool_inputs(sources: Mapping[str, bytes]) -> list[str]:
    """The ``quality_gates`` files the runner and the proof tool import, at any depth, and the recorder's own files.

    Breadth-first over every import statement, those inside functions too: the
    runner imports siblings by bare name and several only inside a function, and a
    change to one of them must move every key. A name that is not a
    ``quality_gates/<name>.py`` file in ``sources`` is a library; its version is in the profile.
    The recorder's files are added by name: its worker is started by path, never imported.
    """
    reached: set[str] = set()
    pending = deque(seed for seed in _TOOL_SEEDS if seed in sources)
    while pending:
        path = pending.popleft()
        if path not in reached:
            reached.add(path)
            pending.extend(sorted(_sibling_imports(path, sources[path], sources) - reached))
    return sorted(reached | {path for path in _RECORDER_INPUTS if path in sources})


def _postgres_identity() -> str:
    """``pg_ctl --version`` of the fixture's first PostgreSQL bin directory; a word when there is none or it cannot be read."""
    try:
        pg_ctl = private_test_pg.pg_binary("pg_ctl")
    except FileNotFoundError:
        return "absent"
    try:
        result = subprocess.run((str(pg_ctl), "--version"), capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unreadable"
    return result.stdout.strip() if result.returncode == 0 else "unreadable"


def tool_profile(root: Path, tree: FrozenTree, sources: Mapping[str, bytes]) -> dict[str, Any]:
    """Interpreter, dependency versions (each installed (name, version) listed once), the closed child environment, PostgreSQL and the files the runner imports."""
    from quality_gates.run_smokes import _CHILD_ENVIRONMENT_KEYS, _import_roots

    dependencies = sorted({(dist.metadata.get("Name", "unknown"), dist.version) for dist in importlib.metadata.distributions()})
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
        "inputs": {path: tree.entries[path] for path in tool_inputs(sources)},
        "solet_name": os.environ.get("SOLET_NAME"),
        "postgres": _postgres_identity(),
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
                    unknown.append(f"dynamic {name} at line {node.lineno} is not proven")
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
    """Inputs repository bytes cannot prove. The environment is not one: the runner's child environment is a closed list in the profile."""
    notices: set[str] = set()
    for node in ast.walk(tree):
        if _external_import(node):
            notices.add("external process/network/database input is not proven")
        if _file_access(node):
            notices.add("file/resource target is not proven by literal references")
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
