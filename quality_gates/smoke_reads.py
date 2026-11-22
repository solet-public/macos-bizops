"""Immutable smoke read observations, root-bound identity and revalidation.

A certificate is created by one worker, read through to_dict/validation, and
corrected by a new run. A stale or incomplete trace is a miss. There is no
persistent store or reuse policy here; callers retire obsolete trace artifacts.
The canonical trace digest covers the root digest as well as observed inputs.
"""

from __future__ import annotations

import hashlib
import io
import json
import marshal
import mmap
import os
import stat
import sys
import sysconfig
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quality_gates import smoke_read_hooks as hooks

READ_SCHEMA = "smoke-reads/v1"
_RULE = "python-recorded-inputs/root-bound-v2"
_LIMIT = 10_000
_MAX_BYTES = 2 * 1024 * 1024
_SECTIONS = ("reads", "metadata", "directories", "absent", "code_inputs", "runtime_inputs", "generated_scratch")
_ATTRIBUTION = {"trace_sha256", "attribution", "complete", "incomplete_reasons"}
_ORIGINAL_OS = {name: getattr(os, name) for name in ("open", "close", "dup", "dup2", "fdopen", "read", "pread", "readv", "lseek", "fstat", "stat", "lstat", "access", "readlink", "listdir", "scandir", "chdir", "fchdir", "getcwd") if hasattr(os, name)}
_OPEN = io.open
_LIBRARY = os.path.realpath(sysconfig.get_path("stdlib"))


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical({key: item for key, item in value.items() if key not in _ATTRIBUTION})).hexdigest()


def _blob(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()


def _file_identity(path: str) -> dict[str, str]:
    info = _ORIGINAL_OS["lstat"](path)
    if stat.S_ISLNK(info.st_mode):
        data = os.fsencode(_ORIGINAL_OS["readlink"](path))
        mode = "120000"
    else:
        with _OPEN(path, "rb") as stream:
            data = stream.read()
        mode = "100755" if info.st_mode & 0o111 else "100644"
    return {"mode": mode, "blob": _blob(data)}


def _stat_value(info: os.stat_result) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name in dir(info):
        if name.startswith("st_"):
            value = getattr(info, name)
            if type(value) not in (int, float):
                raise ValueError(f"unsupported-stat-field:{name}")
            fields[name] = {"type": type(value).__name__, "value": value}
    return {"tuple": list(info), "fields": fields}


@dataclass(frozen=True)
class ReadTrace:
    """One sealed observation; attribution is outside its dependency identity."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(_canonical(self.payload))


@dataclass
class _State:
    root: str
    scratch: str
    payload: dict[str, Any]
    rows: dict[str, dict[str, dict[str, Any]]] = field(default_factory=lambda: {name: {} for name in _SECTIONS})
    reasons: set[str] = field(default_factory=set)
    identities: dict[str, dict[str, str]] = field(default_factory=dict)
    codes: set[types.CodeType] = field(default_factory=set)
    runtime_files: dict[str, str] = field(default_factory=dict)
    instrumentation: hooks.HookState | None = None
    sealed: ReadTrace | None = None
    source_codes: dict[str, set[str]] = field(default_factory=dict)


def _miss(state: _State, reason: str) -> None:
    state.reasons.add(reason)


def _row(state: _State, section: str, row: dict[str, Any]) -> None:
    try:
        key = _canonical(row).decode()
    except (TypeError, ValueError, UnicodeError) as exc:
        _miss(state, "unsupported-observation:" + type(exc).__name__)
        return
    if len(state.rows[section]) >= _LIMIT:
        _miss(state, "trace-overflow")
    else:
        state.rows[section][key] = row


def _relative(state: _State, path: str) -> str | None:
    if path == state.root:
        return "."
    prefix = state.root + os.sep
    return path[len(prefix) :].replace(os.sep, "/") if path.startswith(prefix) else None


def _symlinks(state: _State, path: str) -> None:
    parts = Path(path).parts
    current = parts[0]
    for part in parts[1:]:
        current = os.path.join(current, part)
        try:
            info = _ORIGINAL_OS["lstat"](current)
        except OSError:
            break
        if stat.S_ISLNK(info.st_mode):
            target = _ORIGINAL_OS["readlink"](current)
            relative = _relative(state, current)
            if relative is not None:
                _row(state, "metadata", {"path": relative, "operation": "readlink", "result": target})
            elif not state.root.startswith(current + os.sep):
                _miss(state, "external-symlink")
    resolved = os.path.realpath(path)
    if _relative(state, path) is not None and _relative(state, resolved) is None:
        _miss(state, "repository-escape")


def _runtime_identity(state: _State, path: str) -> bool:
    path = os.path.realpath(path)
    if path in state.runtime_files:
        return True
    library = _LIBRARY
    if not path.startswith(library + os.sep) or "site-packages" in path.split(os.sep):
        return False
    try:
        identity = _file_identity(path)
    except OSError:
        return False
    state.runtime_files[path] = identity["blob"]
    _row(state, "runtime_inputs", {"path": "@stdlib/" + os.path.relpath(path, library), **identity})
    return True


def _observe_file(state: _State, path: str | None) -> None:
    if path is None:
        return
    _symlinks(state, path)
    path = os.path.realpath(path)
    relative = _relative(state, path)
    if relative is None:
        if not _runtime_identity(state, path):
            _miss(state, "external-file")
        return
    try:
        identity = _file_identity(path)
    except OSError as exc:
        _row(state, "absent", {"path": relative, "operation": "open", "errno": exc.errno})
        return
    previous = state.identities.setdefault(path, identity)
    if previous != identity:
        _miss(state, "unstable-input:" + relative)
    _row(state, "reads", {"path": relative, **identity})


def _observation(state: _State, path: str | None, operation: str, result: Any = None, error: OSError | None = None, **options: Any) -> None:
    if path is None:
        return
    relative = _relative(state, path)
    if relative is None:
        _miss(state, "external-metadata")
        return
    _symlinks(state, path)
    row = {"path": relative, "operation": operation, **options}
    row.update({"errno": error.errno} if error else {"result": result})
    section = "directories" if operation in ("listdir", "scandir") else "absent" if error or operation == "exists" and result is False else "metadata"
    _row(state, section, row)


def _code_digest(code: types.CodeType, filename: str) -> str:
    constants = tuple(_normalized_code(item, filename) if isinstance(item, types.CodeType) else item for item in code.co_consts)
    normalized = code.replace(co_filename=filename, co_consts=constants)
    return hashlib.sha256(marshal.dumps(normalized)).hexdigest()


def _normalized_code(code: types.CodeType, filename: str) -> types.CodeType:
    constants = tuple(_normalized_code(item, filename) if isinstance(item, types.CodeType) else item for item in code.co_consts)
    return code.replace(co_filename=filename, co_consts=constants)


def _collect_codes(code: types.CodeType, filename: str) -> set[str]:
    result = {_code_digest(code, filename)}
    for item in code.co_consts:
        if isinstance(item, types.CodeType):
            result.update(_collect_codes(item, filename))
    return result


def _is_frozen(filename: str) -> bool:
    for name, module in tuple(sys.modules.items()):
        if filename == "<frozen " + name + ">" and getattr(getattr(module, "__spec__", None), "origin", None) == "frozen":
            return True
    return False


def _check_source_code(state: _State, code: types.CodeType, path: str) -> None:
    normalized = _relative(state, path) or "@stdlib/" + os.path.relpath(os.path.realpath(path), _LIBRARY)
    observed = _code_digest(code, normalized)
    if path not in state.source_codes:
        with _OPEN(path, "rb") as stream:
            source = stream.read()
        compiled = compile(source, path, "exec", dont_inherit=True, optimize=sys.flags.optimize)
        state.source_codes[path] = _collect_codes(compiled, normalized)
    if observed not in state.source_codes[path]:
        _miss(state, "unowned-code")
    _row(state, "code_inputs", {"path": normalized, "code_sha256": observed})
    state.codes.add(code)


def _code_identity(state: _State, code: types.CodeType) -> None:
    if code in state.codes:
        return
    filename = code.co_filename
    if _is_frozen(filename):
        state.codes.add(code)
        return
    if filename.startswith("<"):
        _miss(state, "opaque-code")
        return
    path = os.path.abspath(filename)
    _observe_file(state, path)
    _check_source_code(state, code, path)


def _runtime_census(state: _State) -> None:
    _row(state, "runtime_inputs", {"path": "@repository_root", "sha256": hashlib.sha256(os.fsencode(state.root)).hexdigest()})
    _row(state, "runtime_inputs", {"path": "@interpreter", **_file_identity(os.path.realpath(sys.executable)), "version": sys.version, "cache_tag": sys.implementation.cache_tag})
    for module in tuple(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if isinstance(filename, str) and (filename.endswith(".py") or module is mmap):
            _runtime_identity(state, os.path.abspath(filename))


def _start(state: _State) -> None:
    _runtime_census(state)
    _observe_file(state, os.path.join(state.root, state.payload["smoke"]))
    instrumentation = hooks.HookState(
        root=state.root,
        capture=lambda path: _observe_file(state, path),
        observe=lambda path, operation, result=None, error=None, **options: _observation(state, path, operation, result, error, **options),
        code=lambda code: _code_identity(state, code),
        relative=lambda path: _relative(state, path),
        reject=lambda reason: _miss(state, reason),
        serialize_stat=_stat_value,
        trusted_files=frozenset({__file__, hooks.__file__}),
    )
    state.instrumentation = instrumentation
    hooks.start(instrumentation)


def _check_integrity(state: _State) -> None:
    if state.instrumentation is not None:
        hooks.check(state.instrumentation)
    for path, identity in state.identities.items():
        try:
            current = _file_identity(path)
        except OSError:
            current = {}
        if current != identity:
            _miss(state, "unstable-input:" + str(_relative(state, path)))


def _finish(state: _State) -> ReadTrace:
    if state.sealed is not None:
        return state.sealed
    assert state.instrumentation is not None
    state.instrumentation.busy += 1
    try:
        _check_integrity(state)
        for section in _SECTIONS:
            state.payload[section] = sorted(state.rows[section].values(), key=lambda row: _canonical(row))
        if sum(len(rows) for rows in state.rows.values()) > _LIMIT or len(_canonical(state.payload)) > _MAX_BYTES:
            _miss(state, "trace-overflow")
        state.payload["incomplete_reasons"] = sorted(state.reasons)
        state.payload["complete"] = not state.reasons
        state.payload["trace_sha256"] = _digest(state.payload)
        state.sealed = ReadTrace(state.payload)
        return state.sealed
    finally:
        state.instrumentation.busy -= 1


class TraceRecorder:
    """Seal one worker's observations; the worker then adds its root probe.

    Validation accepts a sealed snapshot only after that diagnostic matched.
    Its root digest remains binding even when a second root behaved equally.
    """

    def __init__(self, root: Path, smoke: str, profile_sha256: str, scratch: Path) -> None:
        resolved = str(root.resolve())
        payload: dict[str, Any] = {"schema": READ_SCHEMA, "rule_version": _RULE, "smoke": smoke, "profile_sha256": profile_sha256, "complete": False, "incomplete_reasons": [], "attribution": {"pid": os.getpid(), "original_path": resolved, "origin_commit": None, "origin_tree": None, "attempt": None}}
        self._state = _State(resolved, str(scratch), payload)

    def __enter__(self) -> TraceRecorder:
        _start(self._state)
        return self

    def seal(self) -> ReadTrace:
        return _finish(self._state)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.seal()
        finally:
            if self._state.instrumentation is not None:
                hooks.stop(self._state.instrumentation)


def _validate_header(trace: Mapping[str, Any], profile_sha256: str) -> list[str]:
    reasons: list[str] = []
    if trace.get("schema") != READ_SCHEMA or trace.get("rule_version") != _RULE:
        reasons.append("unsupported-schema-or-rule")
    if trace.get("complete") is not True or trace.get("incomplete_reasons") != []:
        reasons.append("incomplete-trace")
    if trace.get("profile_sha256") != profile_sha256:
        reasons.append("profile-mismatch")
    if trace.get("trace_sha256") != _digest(trace):
        reasons.append("trace-digest-mismatch")
    if any(not isinstance(trace.get(section), list) for section in _SECTIONS):
        reasons.append("missing-sections")
    return reasons


def _safe_join(root: Path, relative: str) -> str:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe-certificate-path")
    return str(root / path)


def _read_observation(path: str, row: Mapping[str, Any]) -> Any:
    operation = row["operation"]
    if operation in ("stat", "fstat", "lstat"):
        name = "lstat" if operation == "lstat" else "stat"
        info = _ORIGINAL_OS[name](path, **({"follow_symlinks": row.get("follow_symlinks", True)} if name == "stat" else {}))
        return _stat_value(info)
    if operation in ("listdir", "scandir"):
        return _ORIGINAL_OS["listdir"](path)
    if operation == "resolve":
        return str(Path(path).resolve())
    if operation == "open":
        with _OPEN(path, "rb"):
            return True
    if operation == "access":
        return _ORIGINAL_OS["access"](path, row["mode"], effective_ids=row.get("effective_ids", False), follow_symlinks=row.get("follow_symlinks", True))
    return getattr(Path(path), operation)() if operation in ("exists", "is_file", "is_dir") else _ORIGINAL_OS[operation](path)


def _validate_observation(root: Path, row: Mapping[str, Any]) -> bool:
    path = _safe_join(root, row["path"])
    try:
        result = _read_observation(path, row)
    except OSError as exc:
        return "errno" in row and exc.errno == row["errno"]
    if "errno" in row:
        return False
    if row["operation"] == "resolve":
        result = os.path.relpath(result, root).replace(os.sep, "/")
    return result == row["result"]


def _validate_reads(trace: Mapping[str, Any], root: Path, entries: Mapping[str, Mapping[str, str]]) -> list[str]:
    reasons: list[str] = []
    for row in trace["reads"]:
        path = row["path"]
        expected = {key: row[key] for key in ("mode", "blob")}
        if path not in entries or any(entries[path].get(key) != value for key, value in expected.items()) or _file_identity(_safe_join(root, path)) != expected:
            reasons.append("read-changed:" + path)
    return reasons


def _validate_bindings(trace: Mapping[str, Any], root: Path) -> list[str]:
    reasons: list[str] = []
    roots = [row for row in trace["runtime_inputs"] if row["path"] == "@repository_root"]
    if roots != [{"path": "@repository_root", "sha256": hashlib.sha256(os.fsencode(root)).hexdigest()}]:
        reasons.append("root-digest-mismatch")
    probes = [row for row in trace["runtime_inputs"] if row["path"] == "@root_probe"]
    if probes != [{"path": "@root_probe", "status": "matched"}]:
        reasons.append("root_probe_unavailable")
    return reasons


def _validate_runtime(trace: Mapping[str, Any], root: Path) -> list[str]:
    reasons = _validate_bindings(trace, root)
    for row in trace["runtime_inputs"]:
        if row["path"] in ("@repository_root", "@root_probe"):
            continue
        path = os.path.realpath(sys.executable) if row["path"] == "@interpreter" else os.path.join(_LIBRARY, row["path"].removeprefix("@stdlib/"))
        if _file_identity(path)["blob"] != row["blob"]:
            reasons.append("runtime-changed")
    return reasons


def _validate_inputs(trace: Mapping[str, Any], root: Path, entries: Mapping[str, Mapping[str, str]]) -> list[str]:
    reasons = _validate_reads(trace, root, entries)
    for section in ("metadata", "directories", "absent"):
        for row in trace[section]:
            if not _validate_observation(root, row):
                reasons.append("observation-changed:" + row["path"])
    reasons.extend(_validate_runtime(trace, root))
    return reasons


def validate_certificate(trace: Mapping[str, Any], *, root: Path, entries: Mapping[str, Mapping[str, str]], profile_sha256: str) -> tuple[bool, list[str]]:
    """Revalidate a complete trace against the current tree and pinned runtime."""
    try:
        reasons = _validate_header(trace, profile_sha256)
        if not reasons:
            reasons.extend(_validate_inputs(trace, root.resolve(), entries))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        reasons = ["invalid-certificate:" + type(exc).__name__]
    return not reasons, reasons
