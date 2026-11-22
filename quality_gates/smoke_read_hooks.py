"""Bound filesystem adapters and conservative interpreter event classification.

This module has no dependency on the certificate API. Recorder callbacks own
normalization, identities and trace rows; these hooks own actual path/fd/cwd
arguments, native callable identity and restoring the worker instrumentation.
"""

from __future__ import annotations

import _io
import builtins
import functools
import importlib.machinery
import io
import os
import posix
import runpy
import stat
import sys
import threading
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ORIGINAL_OS = {name: getattr(os, name) for name in ("open", "close", "dup", "dup2", "fdopen", "read", "pread", "readv", "lseek", "fstat", "stat", "lstat", "access", "readlink", "listdir", "scandir", "chdir", "fchdir", "getcwd") if hasattr(os, name)}
_PATH_TYPES = (str, bytes, Path, type(Path()))
_IMPORT_CODES = {value.__code__ for value in vars(runpy).values() if isinstance(value, types.FunctionType)}
_IMPORT_CALLS = {dict.copy, os.getcwd}


@dataclass
class HookState:
    """One installation's callbacks and descriptor provenance; no global owner."""

    root: str
    capture: Callable[[str | None], None]
    observe: Callable[..., None]
    code: Callable[[types.CodeType], None]
    relative: Callable[[str], str | None]
    reject: Callable[[str], None]
    serialize_stat: Callable[[os.stat_result], dict[str, Any]]
    trusted_files: frozenset[str]
    descriptors: dict[int, str] = field(default_factory=dict)
    patches: list[tuple[Any, str, Any, Any]] = field(default_factory=list)
    pure: set[Any] = field(default_factory=set)
    busy: int = 0
    active: bool = False
    owner_thread: int = 0
    old_profile: Any = None
    profile: Any = None


def _bind(state: HookState, argument: Any, dir_fd: int | None = None) -> str | None:
    if isinstance(argument, int):
        path = state.descriptors.get(argument)
        if path is None:
            state.reject(f"unbound-fd:{argument}")
        return path
    if type(argument) not in _PATH_TYPES:
        state.reject("unsupported-path-argument")
        return None
    value = os.fsdecode(argument)
    if os.path.isabs(value):
        return os.path.normpath(value)
    base = _ORIGINAL_OS["getcwd"]() if dir_fd is None else state.descriptors.get(dir_fd)
    if base is None:
        state.reject(f"unbound-dir-fd:{dir_fd}")
        return None
    # A lexical '..' after a symlink has filesystem semantics, not normpath's.
    if ".." in value.split(os.sep):
        state.reject("parent-traversal-path")
    return os.path.normpath(os.path.join(base, value))


def _call_argument(args: tuple[Any, ...], kwargs: dict[str, Any], index: int, name: str, default: Any = None) -> Any:
    return args[index] if len(args) > index else kwargs.get(name, default)


def _bind_dup2_target(state: HookState, target_fd: Any, path: str | None) -> None:
    state.descriptors.pop(target_fd, None)
    if path is not None:
        state.descriptors[target_fd] = path


def _file_after(state: HookState, operation: str, result: Any, path: str | None, argument: Any, target_fd: Any = None) -> None:
    if operation == "open" and isinstance(result, int) and path is not None:
        state.descriptors[result] = path
    elif operation == "dup" and path is not None:
        state.descriptors[result] = path
    elif operation == "dup2":
        _bind_dup2_target(state, target_fd, path)
    elif operation == "close":
        state.descriptors.pop(argument, None)
    elif operation in ("io.open", "builtins.open", "open_code", "fdopen") and path is not None:
        state.descriptors[result.fileno()] = path


def _file_call(state: HookState, operation: str, original: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    custom_opener = operation in ("io.open", "builtins.open") and _call_argument(args, kwargs, 7, "opener") is not None
    if custom_opener:
        state.reject("unsupported:custom-opener")
    argument = _call_argument(args, kwargs, 0, "file" if operation in ("io.open", "builtins.open") else "path" if operation in ("open", "open_code") else "fd")
    path = _bind(state, argument, kwargs.get("dir_fd"))
    _observe_bound_read(state, operation, path, args, kwargs)
    try:
        result = original(*args, **kwargs)
    except OSError as exc:
        state.observe(path, "open", error=exc)
        raise
    if not custom_opener:
        _file_after(state, operation, result, path, argument, _call_argument(args, kwargs, 1, "fd2") if operation == "dup2" else None)
    return result


def _observe_bound_read(state: HookState, operation: str, path: str | None, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    if operation in ("close", "dup", "dup2", "lseek"):
        return
    if operation == "open":
        flags = _call_argument(args, kwargs, 1, "flags", 0)
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            state.reject("filesystem-write")
    if operation in ("io.open", "builtins.open", "fdopen"):
        mode = _call_argument(args, kwargs, 1, "mode", "r")
        if any(flag in str(mode) for flag in "wax+"):
            state.reject("filesystem-write")
    if path is not None:
        try:
            if stat.S_ISDIR(_ORIGINAL_OS["stat"](path).st_mode):
                return
        except OSError:
            pass
    state.capture(path)


def _metadata_result(state: HookState, operation: str, result: Any) -> Any:
    return state.serialize_stat(result) if operation in ("stat", "lstat", "fstat") else result


def _metadata_call(state: HookState, operation: str, original: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    argument = _call_argument(args, kwargs, 0, "fd" if operation == "fstat" else "path")
    path = _bind(state, argument, kwargs.get("dir_fd"))
    try:
        result = original(*args, **kwargs)
    except OSError as exc:
        state.observe(path, operation, error=exc, follow_symlinks=kwargs.get("follow_symlinks", True))
        raise
    try:
        observed = _metadata_result(state, operation, result)
        options = {"follow_symlinks": kwargs.get("follow_symlinks", True)}
        if operation == "access":
            options.update(mode=_call_argument(args, kwargs, 1, "mode"), effective_ids=kwargs.get("effective_ids", False))
        state.observe(path, operation, observed, **options)
    except ValueError as exc:
        state.reject(str(exc))
    return result


def _directory_call(state: HookState, operation: str, original: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    argument = _call_argument(args, kwargs, 0, "path", ".")
    path = _bind(state, argument)
    try:
        result = original(*args, **kwargs)
    except OSError as exc:
        state.observe(path, operation, error=exc)
        raise
    if operation == "listdir":
        state.observe(path, operation, result)
    elif operation == "scandir":
        # DirEntry caches native metadata and methods cannot be rebound safely.
        state.reject("uncovered-direntry")
        state.observe(path, operation, _ORIGINAL_OS["listdir"](path))
    else:
        state.reject("cwd-observation")
    return result


def _path_call(state: HookState, operation: str, original: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    path = _bind(state, args[0])
    try:
        result = original(*args, **kwargs)
    except OSError as exc:
        state.observe(path, operation, error=exc)
        raise
    observed = str(result) if operation == "resolve" else result
    if operation == "resolve" and path is not None:
        state.observe(path, "resolve", state.relative(os.path.realpath(path)) or "@outside")
        resolved = state.relative(str(result))
        observed = resolved if resolved is not None else "@outside"
        if resolved is None:
            state.reject("repository-escape")
    state.observe(path, operation, observed)
    return result


def _wrapped(state: HookState, operation: str, original: Callable[..., Any], adapter: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if state.busy or not state.active:
            return original(*args, **kwargs)
        state.busy += 1
        try:
            return adapter(state, operation, original, args, kwargs)
        finally:
            state.busy -= 1

    return wrapper


def _patch(state: HookState, owner: Any, name: str, operation: str, adapter: Callable[..., Any]) -> None:
    original = getattr(owner, name)
    wrapper = _wrapped(state, operation, original, adapter)
    state.patches.append((owner, name, original, wrapper))
    setattr(owner, name, wrapper)


def _install_files(state: HookState) -> None:
    for name in ("open", "close", "dup", "dup2", "fdopen", "read", "pread", "readv", "lseek"):
        if hasattr(os, name):
            _patch(state, os, name, name, _file_call)
    for name in ("stat", "lstat", "fstat", "access", "readlink"):
        _patch(state, os, name, name, _metadata_call)
    for name in ("listdir", "scandir", "chdir", "fchdir"):
        _patch(state, os, name, name, _directory_call)
    _patch(state, builtins, "open", "builtins.open", _file_call)
    _patch(state, io, "open", "io.open", _file_call)
    _patch(state, io, "open_code", "open_code", _file_call)
    _patch(state, _io, "open_code", "open_code", _file_call)
    _patch(state, _io, "open", "io.open", _file_call)
    # Importlib binds the native posix module, rather than the public os facade.
    for name in ("stat", "lstat"):
        _patch(state, posix, name, name, _metadata_call)
    for name in ("exists", "is_file", "is_dir", "resolve"):
        _patch(state, Path, name, name, _path_call)


def _audit(state: HookState, event: str, args: tuple[Any, ...]) -> None:
    if not state.active or state.busy:
        return
    state.busy += 1
    try:
        _audit_event(state, event, args)
    except Exception as exc:  # Recording failure removes certification, not execution.
        state.reject("recording-error:" + type(exc).__name__)
    finally:
        state.busy -= 1


def _audit_event(state: HookState, event: str, args: tuple[Any, ...]) -> None:
    if event == "exec":
        state.code(args[0])
    elif event == "compile":
        filename = args[1]
        if not isinstance(filename, str) or filename.startswith("<"):
            state.reject("unbound-compile")
    elif event == "import":
        _import_search(state, args)
    elif event in ("sys.setprofile", "sys.settrace", "sys.addaudithook"):
        state.reject("instrumentation-change:" + event)
    elif event == "open":
        # A direct C open not passing through a bound file adapter has no fd.
        state.reject("unbound-open")
        state.capture(_bind(state, args[0]))
    elif event == "mmap.__new__":
        state.capture(_bind(state, args[0]))
    elif event.startswith(("socket.", "subprocess.", "ctypes.", "os.exec", "os.fork", "os.posix_spawn", "os.system", "os.putenv", "os.unsetenv")):
        state.reject("unsupported-action:" + event)


def _import_search(state: HookState, args: tuple[Any, ...]) -> None:
    name = str(args[0])
    filename = args[1]
    if filename:
        state.capture(_bind(state, filename))
        if any(str(filename).endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES):
            state.reject("native-extension:" + name)
    for entry in sys.path:
        _record_search_directory(state, entry)


def _record_search_directory(state: HookState, entry: Any) -> None:
    if type(entry) is not str:
        state.reject("unbound-import-path")
        return
    path = _bind(state, entry or ".")
    if path is None or state.relative(path) is None:
        return
    try:
        names = _ORIGINAL_OS["listdir"](path)
    except OSError as exc:
        state.observe(path, "listdir", error=exc)
    else:
        state.observe(path, "listdir", names)


def _pure_calls() -> set[Any]:
    calls: set[Any] = {builtins.compile, builtins.exec, builtins.hasattr, builtins.isinstance, builtins.len, builtins.max, builtins.print, os.fspath, stat.S_ISLNK, stat.S_ISREG, sys.exit, sys.intern}
    calls.update(getattr(posix, name) for name in ("_path_normpath", "_path_splitroot_ex") if hasattr(posix, name))
    for owner, names in ((bytes, ("rfind",)), (dict, ("update",)), (list, ("append", "extend", "pop")), (object, ("__new__",)), (str, ("endswith", "join", "rpartition", "split", "startswith"))):
        calls.update(getattr(owner, name) for name in names)
    return calls


def _call_identity(call: Any) -> Any:
    owner = getattr(call, "__self__", None)
    name = getattr(call, "__name__", "")
    return getattr(type(owner), name, call) if owner is not None and not isinstance(owner, (types.ModuleType, type)) else call


def _bound_stream(state: HookState, call: Any) -> bool:
    owner: Any = getattr(call, "__self__", None)
    if type(owner) not in (io.BufferedReader, io.FileIO, io.TextIOWrapper):
        return False
    if getattr(call, "__name__", "") not in ("__exit__", "read", "seek", "tell", "close", "fileno"):
        return False
    try:
        fd = owner.fileno()
    except ValueError:
        return False
    path = state.descriptors.get(fd)
    if path is None:
        state.reject("unbound-file-object")
    else:
        state.capture(path)
    return True


def _profile_call(state: HookState, frame: types.FrameType, event: str, arg: Any) -> None:
    if event == "call":
        state.code(frame.f_code)
    elif event == "c_call":
        identity = _call_identity(arg)
        machinery_call = frame.f_code in _IMPORT_CODES and identity in _IMPORT_CALLS
        if identity not in state.pure and not machinery_call and not _bound_stream(state, arg):
            state.reject("unclassified-call:" + str(getattr(arg, "__module__", "")) + "." + str(getattr(arg, "__qualname__", "unknown")))


def _profiler(state: HookState) -> Callable[..., None]:
    def profile(frame: types.FrameType, event: str, arg: Any) -> None:
        if not state.active or state.busy or frame.f_code.co_filename in state.trusted_files:
            return
        state.busy += 1
        try:
            if threading.get_ident() != state.owner_thread:
                state.reject("uncovered-thread")
            _profile_call(state, frame, event, arg)
        except Exception as exc:  # Opaque code or metadata still executes normally.
            state.reject("recording-error:" + type(exc).__name__)
        finally:
            state.busy -= 1

    return profile


def start(state: HookState) -> None:
    """Install adapters after the recorder has pinned its own runtime inputs."""
    state.busy += 1
    try:
        state.owner_thread = threading.get_ident()
        state.old_profile = sys.getprofile()
        if state.old_profile is not None or threading.active_count() != 1:
            state.reject("preexisting-instrumentation-or-thread")
        state.pure = _pure_calls()
        _install_files(state)
        state.profile = _profiler(state)
        sys.addaudithook(lambda event, args: _audit(state, event, args))
        state.active = True
        sys.setprofile(state.profile)
    finally:
        state.busy -= 1


def check(state: HookState) -> None:
    """Disclose replacement even if the smoke installed its original hook back."""
    if sys.getprofile() is not state.profile:
        state.reject("profiling-hook-changed")
    if threading.active_count() != 1:
        state.reject("uncovered-thread")
    for owner, name, _original, wrapper in state.patches:
        if getattr(owner, name) is not wrapper:
            state.reject("instrumentation-replaced:" + name)


def stop(state: HookState) -> None:
    """Restore only this installation's hooks after the final observation."""
    state.busy += 1
    try:
        state.active = False
        sys.setprofile(state.old_profile)
        for owner, name, original, _wrapper in reversed(state.patches):
            setattr(owner, name, original)
    finally:
        state.busy -= 1
