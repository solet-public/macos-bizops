"""Owned filesystem and transport fixtures for the advisory fast-return census.

Handlers are never offloaded or refused by production code here. During an
explicit census measurement, host transports answer through the existing
probe, thread targets remain parked, and filesystem writes must resolve under
one caller-owned temporary home. Code and verified interpreter dependencies
remain readable; real profile, credential and coding-agent data stay protected.

Descriptor-relative operands retain their actual syscall arguments. The audit
binds directory handles through macOS F_GETPATH and physical device/inode
identity, or an explicitly recorded identity search beneath the owned home.
This covers open, cleanup, metadata changes and both operands of rename/link.
Denied operations remain recorded and never reach the filesystem syscall.

Each temporary home owns its data/config directories and file log handlers.
Normal completion and exception unwinding close those handlers before the
context manager removes the directory. Reports and checkpoints belong to the
caller and are retained separately from these disposable execution fixtures.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import linecache
import logging
import os
import platform
import socket
import stat
import struct
import subprocess
import sys
import sysconfig
import tempfile
import threading
import zoneinfo
from collections.abc import Callable, Generator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never, Protocol
from unittest.mock import patch

if TYPE_CHECKING:
    from quality_gates.private_test_pg import FixtureConfig


def citation_definition(root: Path, citation: str) -> ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef:
    """Resolve the qualified symbol; line hints cannot block a valid source."""
    location, symbol = citation.rsplit("#", 1)
    path, raw_line = location.rsplit(":", 1)
    source = (root / path).resolve()
    assert source.is_relative_to((root / "plugins").resolve()), citation
    assert source.is_file(), f"missing citation file: {citation}"
    node = citation_symbol(ast.parse(source.read_text(encoding="utf-8"), filename=str(source)), symbol)
    try:
        hint = int(raw_line)
    except ValueError:
        hint = 0
    start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
    if not start <= hint <= (node.end_lineno or node.lineno):
        print(f"ADVISORY citation line hint drifted: {citation}; symbol is at {path}:{node.lineno}")
    return node


def citation_symbol(tree: ast.AST, symbol: str) -> ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef:
    """Resolve every qualified source name within its lexical parent."""
    node = tree
    for part in symbol.split("."):
        matches = [child for child in ast.iter_child_nodes(node) if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == part]
        assert len(matches) == 1, f"missing or ambiguous citation symbol: {symbol}"
        node = matches[0]
    assert isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)), symbol
    return node


class BoundaryProbe(Protocol):
    """The census owns timing/observations; fixtures only call its boundary."""

    starts: list[str]

    def boundary(self, name: str, *, socket_error: bool = False) -> Callable[..., Any]: ...


_HOST_HOME = Path.home().resolve()

_active_guard: _AuditGuard | None = None

_audit_installed = False


@dataclass(frozen=True)
class _FileCall:
    event: str
    operands: tuple[Any, ...]
    directory_fds: tuple[int | None, ...]


_file_call: ContextVar[_FileCall | None] = ContextVar("census_file_call", default=None)


class BoundaryAnswer(BaseException):
    """Terminal fixture transport answer; cannot be swallowed by SDK retry loops."""


class UnsafeAttempt(BaseException):
    """An uninstrumented host effect was refused before execution."""


@dataclass
class _AuditGuard:
    """The active call's permitted code/library roots and writable scratch.

    The guard records attempted file operations before checking containment.
    A refused path remains diagnostic evidence; the underlying operation
    has not run. Transport fakes are the primary interception mechanism.
    """

    read_roots: tuple[Path, ...]
    scratch: Path
    protected_roots: tuple[Path, ...] = ()
    library_read_roots: tuple[Path, ...] = ()
    blocked: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    loopback_listener: bool = False
    listener_ports: list[int] = field(default_factory=list)
    devnull_write: bool = False

    def check_path(self, value: object, write: bool, *, traversal: bool = False) -> None:
        if isinstance(value, int) and write:
            self.refuse(f"write descriptor has no isolated path binding: {value}")
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(value)).resolve()
        if _within(path, self.protected_roots) and not self._library_read(path, write):
            self.refuse(f"protected host path is not a census fixture: {path}")
        roots = (self.scratch,) if write else self.read_roots
        if _within(path, roots):
            return
        if _ancestor_traversal(path, roots, write, traversal):
            return
        self.refuse(f"{'write' if write else 'read'} outside isolated fixture/code roots: {path}")

    def refuse(self, message: str) -> Never:
        self.blocked.append(message)
        raise UnsafeAttempt(message)

    def _library_read(self, path: Path, write: bool) -> bool:
        return not write and _within(path, self.library_read_roots)


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _ancestor_traversal(path: Path, roots: tuple[Path, ...], write: bool, traversal: bool) -> bool:
    return traversal and not write and any(root.is_relative_to(path) for root in roots)


_READ_EVENTS = frozenset({"os.listdir", "os.scandir"})

_WRITE_EVENTS = frozenset({"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate", "os.utime"})

_PAIR_EVENTS = frozenset({"os.rename", "os.link", "os.symlink"})

_FILE_EVENTS = _READ_EVENTS | _WRITE_EVENTS | _PAIR_EVENTS | {"open"}

_AUDIT_DESCRIPTOR_FIELDS: dict[str, tuple[int | None, ...]] = {"os.remove": (1,), "os.rmdir": (1,), "os.mkdir": (2,), "os.chmod": (2,), "os.utime": (3,), "os.rename": (2, 3), "os.link": (2, 3), "os.symlink": (None, 2)}

# Path names and directory-fd keyword names follow the Python 3.13 os APIs.
_PATH_OPERATIONS: dict[str, tuple[str, tuple[str, ...], tuple[str | None, ...]]] = {
    "open": ("open", ("path",), ("dir_fd",)),
    "mkdir": ("os.mkdir", ("path",), ("dir_fd",)),
    "unlink": ("os.remove", ("path",), ("dir_fd",)),
    "remove": ("os.remove", ("path",), ("dir_fd",)),
    "rmdir": ("os.rmdir", ("path",), ("dir_fd",)),
    "chmod": ("os.chmod", ("path",), ("dir_fd",)),
    "truncate": ("os.truncate", ("path",), (None,)),
    "utime": ("os.utime", ("path",), ("dir_fd",)),
    "rename": ("os.rename", ("src", "dst"), ("src_dir_fd", "dst_dir_fd")),
    "replace": ("os.rename", ("src", "dst"), ("src_dir_fd", "dst_dir_fd")),
    "link": ("os.link", ("src", "dst"), ("src_dir_fd", "dst_dir_fd")),
    "symlink": ("os.symlink", ("src", "dst"), (None, "dir_fd")),
}


def _open_writes(args: tuple[Any, ...]) -> bool:
    """Interpret both textual open modes and descriptor-level write flags.

    Python's audit tuple can describe either interface. A read permission
    never grants writes to code or library roots; the caller-owned scratch
    directory is the sole write root while a measurement guard is active.
    """
    mode, flags = args[1:3]
    write_mode = isinstance(mode, str) and any(char in mode for char in "wax+")
    write_flags = isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    return write_mode or write_flags


def _audit_file(guard: _AuditGuard, event: str, args: tuple[Any, ...]) -> None:
    """Record one file event and check every path the event can mutate.

    Directory descriptors come from the matching wrapper call or the native
    audit tuple. os.open omits its dir_fd from that tuple, so an unbound
    relative descriptor write has no proven target and is refused. Original
    syscall arguments are preserved; audit resolution never redirects a call.
    """
    value = args[0]
    path = os.fsdecode(value) if isinstance(value, (str, bytes, os.PathLike)) else str(value)
    write = _open_writes(args) if event == "open" else event not in _READ_EVENTS
    record: dict[str, Any] = {"operation": event, "path": path, "paths": [], "write": write, "allowed": False, "thread_id": threading.get_ident()}
    guard.files.append(record)
    if guard.devnull_write and _devnull_write(event, args):
        record.update(paths=[os.devnull], allowed=True, device_sink=True)
        return
    call = _matching_file_call(event, args)
    if _unbound_relative_write(event, args, call, write):
        guard.refuse("relative descriptor write has no audited dir_fd binding")
    traversal = not write and _directory_traversal_open(event, args)
    record["traversal_only"] = traversal
    for target in _audit_targets(guard, event, args, call, record):
        record["paths"].append(str(target))
        guard.check_path(target, write, traversal=traversal)
    if write and not record["paths"]:
        guard.refuse("write operand has no isolated path binding")
    record["allowed"] = True


def _devnull_write(event: str, args: tuple[Any, ...]) -> bool:
    """Admit only a literal device open with no read access or persistent data."""
    if event != "open" or not isinstance(args[0], str | bytes) or os.fsdecode(args[0]) != os.devnull:
        return False
    flags = args[2]
    return isinstance(flags, int) and flags & os.O_ACCMODE == os.O_WRONLY and stat.S_ISCHR(os.stat(os.devnull).st_mode)


def _directory_traversal_open(event: str, args: tuple[Any, ...]) -> bool:
    """Directory descriptors cannot read file content; listing is audited separately."""
    if event != "open" or args[1] is not None:
        return False
    flags = args[2]
    return isinstance(flags, int) and all(flags & getattr(os, name, 0) for name in ("O_DIRECTORY", "O_NOFOLLOW"))


def _matching_file_call(event: str, args: tuple[Any, ...]) -> _FileCall | None:
    call = _file_call.get()
    if call is not None and call.event == event and call.operands == args[: len(call.operands)]:
        _file_call.set(None)
        return call
    return None


def _unbound_relative_write(event: str, args: tuple[Any, ...], call: _FileCall | None, write: bool) -> bool:
    return event == "open" and write and args[1] is None and call is None and isinstance(args[0], (str, bytes, os.PathLike)) and not Path(os.fsdecode(args[0])).is_absolute()


def _audit_targets(guard: _AuditGuard, event: str, args: tuple[Any, ...], call: _FileCall | None, record: dict[str, Any]) -> list[Path]:
    count = 2 if event in _PAIR_EVENTS else 1
    fields = _AUDIT_DESCRIPTOR_FIELDS.get(event, (None,) * count)
    descriptors = call.directory_fds if call is not None else tuple(args[index] if index is not None and args[index] >= 0 else None for index in fields)
    record["directory_fds"] = list(descriptors)
    if event == "os.symlink":
        destination = _resolved_operand(guard, args[1], descriptors[1], record)
        if destination is None:
            guard.refuse("symlink destination has no isolated path binding")
        source = Path(os.fsdecode(args[0]))
        return [(destination.parent / source).resolve(), destination]
    return [target for value, descriptor in zip(args[:count], descriptors, strict=True) if (target := _resolved_operand(guard, value, descriptor, record)) is not None]


def _resolved_operand(guard: _AuditGuard, value: object, descriptor: int | None, record: dict[str, Any]) -> Path | None:
    if isinstance(value, int):
        return _descriptor_path(guard, value, record)
    if not isinstance(value, (str, bytes, os.PathLike)):
        return None
    target = Path(os.fsdecode(value))
    if not target.is_absolute() and descriptor is not None:
        target = _descriptor_path(guard, descriptor, record) / target
    return target.resolve()


def _audit(event: str, args: tuple[Any, ...]) -> None:
    """Apply the process audit hook only during an isolated context.

    Audit hooks cannot be unregistered, so the module leaves this callback
    installed with an inactive guard after use. Nested contexts restore the
    preceding guard. Uninstrumented transports are refused before execution.
    """
    guard = _active_guard
    if guard is None:
        return
    if event in _FILE_EVENTS:
        _audit_file(guard, event, args)
    if event == "socket.bind" and guard.loopback_listener and args[1] == ("127.0.0.1", 0):
        return
    if event.startswith(("socket.connect", "socket.bind", "socket.getaddrinfo", "subprocess.Popen", "os.system", "os.posix_spawn", "os.kill", "os.fork")):
        guard.refuse(f"uninstrumented host boundary: {event}")


def library_roots() -> tuple[Path, ...]:
    """Include active interpreter libraries and their linked dependency stores.

    The native smoke runner owns a private venv with rehomed first-party
    packages. Third-party distributions are symlinked individually to the
    installed dependency store, so resolving sys.prefix alone does not cover
    their physical source and bytecode paths. Read permission follows those
    links only to a site-packages or dist-packages parent.

    This does not admit arbitrary link targets or another first-party tree.
    Plugin class origins still have to belong to the measured repository.
    Writes remain confined to the caller's scratch even for library files.
    """
    sites = {Path(sysconfig.get_path(name)).resolve() for name in ("purelib", "platlib")}
    linked = {entry.resolve().parent for site in sites if site.is_dir() for entry in site.iterdir() if entry.is_symlink()}
    return tuple(sorted(sites | {path for path in linked if path.name in {"site-packages", "dist-packages"}}))


def _owned_descriptor_path(guard: _AuditGuard, identity: os.stat_result) -> Path:
    """Find an fd's physical object only within the owned home, without links."""
    for parent, _directories, _files in os.walk(guard.scratch, followlinks=False):
        for candidate in (Path(parent), *(Path(parent) / name for name in _files)):
            actual = candidate.stat(follow_symlinks=False)
            if (actual.st_dev, actual.st_ino) == (identity.st_dev, identity.st_ino):
                return candidate
    guard.refuse("descriptor identity is absent from the isolated fixture")


def _descriptor_path(guard: _AuditGuard, descriptor: int, record: dict[str, Any]) -> Path:
    """Resolve the actual fd object, retaining evidence of the permitted fallback."""
    binding: dict[str, Any] = {"fd": descriptor, "method": "F_GETPATH", "path": None}
    record.setdefault("descriptor_resolutions", []).append(binding)
    try:
        identity = os.fstat(descriptor)
        raw = fcntl.fcntl(descriptor, fcntl.F_GETPATH, b"\0" * 1024)
        target = Path(os.fsdecode(raw.split(b"\0", 1)[0])).resolve()
        actual = target.stat()
        if (actual.st_dev, actual.st_ino) != (identity.st_dev, identity.st_ino):
            raise OSError("F_GETPATH and descriptor physical identities differ")
    except OSError as error:
        binding.update(method="owned fstat scan", fallback_reason=str(error))
        try:
            target = _owned_descriptor_path(guard, os.fstat(descriptor))
        except OSError as invalid:
            guard.refuse(f"descriptor cannot be bound to the isolated fixture: {invalid}")
    binding["path"] = str(target)
    return target


def _isolated_path_operation(operation: Callable[..., Any], name: str) -> Callable[..., Any]:
    """Bind operands/fds for one native audit event without changing its syscall."""
    event, path_names, directory_names = _PATH_OPERATIONS[name]

    def invoke(*args: Any, **kwargs: Any) -> Any:
        if _active_guard is None:
            return operation(*args, **kwargs)
        positional = list(args)
        operands: list[Any] = []
        for index, parameter in enumerate(path_names):
            value = positional[index] if index < len(positional) else kwargs[parameter]
            value = value if isinstance(value, int) else os.fspath(value)
            operands.append(value)
            if index < len(positional):
                positional[index] = value
            else:
                kwargs[parameter] = value
        call = _FileCall(event, tuple(operands), tuple(kwargs.get(parameter) if parameter is not None else None for parameter in directory_names))
        token = _file_call.set(call)
        try:
            return operation(*positional, **kwargs)
        finally:
            _file_call.reset(token)

    return invoke


def private_pg_environment(config: FixtureConfig) -> dict[str, str]:
    """Pin native libpq defaults to a manifest whose custody was validated."""
    return {"PGHOST": config.host, "PGPORT": str(config.port), "PGDATABASE": config.database, "PGUSER": config.user, "PGSSLMODE": "disable", "PGPASSFILE": os.devnull, "PGSERVICEFILE": os.devnull}


def runtime_environment(scratch: Path) -> dict[str, str]:
    """Use supported timezone and library-cache settings within one owned home."""
    return {"TZ": "UTC", "PYTHONTZPATH": str(scratch / "zoneinfo"), "NUMBA_CACHE_DIR": str(scratch / "cache/numba"), "LIBROSA_CACHE_DIR": str(scratch / "cache/librosa")}


@contextlib.contextmanager
def owned_timezone(scratch: Path) -> Generator[None]:
    """Read real UTC TZif data from scratch and restore the process search path.

    TZif version 1 has no transitions and one zero-offset standard-time type.
    The stdlib's supported search-path API consumes these generated bytes;
    neither a host timezone file nor a substituted ZoneInfo object is used.
    """
    directory = scratch / "zoneinfo"
    directory.mkdir()
    (directory / "UTC").write_bytes(b"TZif\0" + bytes(15) + struct.pack(">6l", 0, 0, 0, 0, 1, 4) + struct.pack(">lbb", 0, 0, 0) + b"UTC\0")
    previous = zoneinfo.TZPATH
    with patch.dict(os.environ, {"PYTHONTZPATH": str(directory)}):
        zoneinfo.reset_tzpath((str(directory),))
        zoneinfo.ZoneInfo.clear_cache(only_keys=["UTC"])
        try:
            yield
        finally:
            zoneinfo.ZoneInfo.clear_cache(only_keys=["UTC"])
            zoneinfo.reset_tzpath(previous)


def _park_thread(thread: threading.Thread) -> None:
    """Keep a parked target inert after global patches and executor cleanup end."""

    def start() -> None:
        return None

    def join(timeout: float | None = None) -> None:
        del timeout

    def alive() -> bool:
        return True

    thread.start = start
    thread.join = join
    thread.is_alive = alive


@contextlib.contextmanager
def audited_setup(root: Path, scratch: Path) -> Generator[_AuditGuard]:
    """Audit real service startup while its explicitly configured threads run."""
    global _active_guard, _audit_installed
    if not _audit_installed:
        sys.addaudithook(_audit)
        _audit_installed = True
    libraries = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), *library_roots())
    guard = _AuditGuard((root.resolve(), scratch.resolve(), *libraries), scratch.resolve(), _protected_roots(root), libraries)
    previous, _active_guard = _active_guard, guard
    try:
        with patch.object(sys, "dont_write_bytecode", True):
            yield guard
    finally:
        _active_guard = previous


@contextlib.contextmanager
def isolated_boundaries(root: Path, scratch: Path, probe: BoundaryProbe, *, postgres: FixtureConfig | None = None, run_threads: frozenset[str] = frozenset(), loopback_listener: bool = False, devnull_write: bool = False) -> Generator[_AuditGuard]:
    """Install fakes before imports, constructors and timed handler calls.

    Environment clearing prevents accidental discovery of live credentials.
    Bytecode writes are disabled and temporary paths point into scratch.
    SDK requests, sockets and subprocesses share the delayed terminal answer.
    Threads report a start without running their target, keeping job workers
    parked. ExitStack restores patches even when BaseException unwinds a fake.
    """
    global _active_guard, _audit_installed
    from quality_gates.private_test_pg import MANIFEST_ENV, load_fixture

    if postgres is None and os.environ.get(MANIFEST_ENV):
        postgres = load_fixture("fast_return_census")
    pg_environment = private_pg_environment(postgres) if postgres is not None else {}
    if not _audit_installed:
        sys.addaudithook(_audit)
        _audit_installed = True
    roots = tuple(dict.fromkeys((root.resolve(), scratch.resolve(), Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), Path(__file__).resolve().parents[2], *library_roots())))
    code_libraries = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), *library_roots())
    guard = _AuditGuard(roots, scratch.resolve(), _protected_roots(root), code_libraries)
    guard.loopback_listener = loopback_listener
    guard.devnull_write = devnull_write
    previous = _active_guard
    census_solet_name = os.environ.get("SOLET_NAME", "census-fixture")
    original_start, original_alive, original_join = threading.Thread.start, threading.Thread.is_alive, threading.Thread.join
    parked: set[threading.Thread] = set()

    def start(thread: threading.Thread) -> None:
        probe.starts.append(thread.name)
        if thread.name in run_threads:
            original_start(thread)
        else:
            parked.add(thread)
            _park_thread(thread)

    def alive(thread: threading.Thread) -> bool:
        return thread in parked or original_alive(thread)

    def join(thread: threading.Thread, timeout: float | None = None) -> None:
        if thread not in parked:
            original_join(thread, timeout)

    original_bind = socket.socket.bind

    def bind(listener: socket.socket, address: Any) -> None:
        if loopback_listener and address == ("127.0.0.1", 0):
            original_bind(listener, address)
            guard.listener_ports.append(int(listener.getsockname()[1]))
            return
        probe.boundary("socket.socket.bind", socket_error=True)()

    class FakePopen(subprocess.Popen[bytes]):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            probe.boundary("subprocess.Popen")()

    targets = (
        "httpx.Client.request",
        "httpx.AsyncClient.request",
        "requests.sessions.Session.request",
        "urllib.request.urlopen",
        "http.client.HTTPConnection.request",
        "botocore.client.BaseClient._make_api_call",
        "googleapiclient.http.HttpRequest.execute",
        "subprocess.run",
        "subprocess.check_output",
        "subprocess.check_call",
        "subprocess.call",
        "os.system",
        "os.posix_spawn",
        "os.posix_spawnp",
    )
    with contextlib.ExitStack() as stack:
        # Importing a third-party library is still guarded. No missing import is
        # silently treated as an installed boundary: the inventory records it.
        _active_guard = guard
        try:
            stack.enter_context(patch.object(sys, "dont_write_bytecode", True))
            stack.enter_context(patch.object(tempfile, "tempdir", str(scratch)))
            for name in _PATH_OPERATIONS:
                stack.enter_context(patch.object(os, name, _isolated_path_operation(getattr(os, name), name)))
            stack.enter_context(patch.dict(os.environ, {"SOLET_NAME": census_solet_name, "APP_HOME": str(scratch), "HOME": str(scratch), "TMPDIR": str(scratch), "PATH": os.environ.get("PATH", ""), **runtime_environment(scratch), **pg_environment}, clear=True))
            stack.enter_context(patch.object(platform, "processor", return_value="census-fixture"))
            stack.enter_context(patch.object(platform, "platform", return_value="Darwin-census-fixture"))
            stack.enter_context(patch.object(platform, "mac_ver", return_value=("0.0", ("", "", ""), "census-fixture")))
            if sys.platform == "darwin":
                stack.enter_context(patch("_osx_support._get_system_version", return_value="11.0"))
                stack.enter_context(patch("_osx_support._get_system_version_tuple", return_value=(11, 0)))
            stack.enter_context(patch.object(subprocess, "Popen", FakePopen))
            # urllib3 probes IPv6 during import and catches OSError. Patch this
            # before importing any transport library, so even that probe has no
            # host effect and its conventional unavailable path remains usable.
            for name in ("connect", "connect_ex", "sendto"):
                stack.enter_context(patch.object(socket.socket, name, probe.boundary(f"socket.socket.{name}")))
            stack.enter_context(patch.object(socket.socket, "bind", bind))
            stack.enter_context(patch.object(socket, "getaddrinfo", probe.boundary("socket.getaddrinfo")))
            for target in targets:
                try:
                    stack.enter_context(patch(target, probe.boundary(target)))
                except (ImportError, AttributeError):
                    continue
            stack.enter_context(patch.object(threading.Thread, "start", start))
            stack.enter_context(patch.object(threading.Thread, "is_alive", alive))
            stack.enter_context(patch.object(threading.Thread, "join", join))
            yield guard
        finally:
            _active_guard = previous


def exception_reason(root: Path, error: Exception) -> str:
    """Name an empty assertion's exact failed prerequisite, retaining its source."""
    trace = error.__traceback__
    if not isinstance(error, AssertionError) or str(error) or trace is None:
        return f"{type(error).__name__}: {error}"
    while trace.tb_next is not None:
        trace = trace.tb_next
    path = Path(trace.tb_frame.f_code.co_filename).resolve()
    statement = linecache.getline(str(path), trace.tb_lineno).strip() if path.is_relative_to(root) else "assertion failed in dependency"
    location = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
    return f"requires isolated prerequisite: {statement} ({location}:{trace.tb_lineno})"


@contextlib.contextmanager
def temporary_app_home(scratch: Path) -> Generator[Path]:
    """Own an empty home beneath caller scratch and remove it on every exit."""
    with tempfile.TemporaryDirectory(prefix="census-app-home-", dir=scratch) as directory:
        home = Path(directory)
        (home / "data").mkdir()
        (home / "config").mkdir()
        try:
            yield home
        finally:
            _close_home_logs(home)


@contextlib.contextmanager
def file_descriptor(path: Path, flags: int = os.O_RDONLY) -> Generator[int]:
    """Own a real descriptor through normal completion and refusal unwinding."""
    descriptor = os.open(path, flags)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def descriptor_directories(scratch: Path) -> Generator[tuple[Path, Path, int, int]]:
    """Own an isolated home and foreign surrogate, including both directory fds."""
    with tempfile.TemporaryDirectory(prefix="census-descriptor-", dir=scratch) as directory:
        home, foreign = Path(directory) / "home", Path(directory) / "foreign"
        home.mkdir()
        foreign.mkdir()
        with file_descriptor(home) as own_fd, file_descriptor(foreign) as foreign_fd:
            yield home, foreign, own_fd, foreign_fd


@dataclass
class DescriptorFixture:
    """Owned descriptor targets and reversible setup for filesystem exercises."""

    home: Path
    foreign: Path
    own_fd: int
    foreign_fd: int
    own_file: int
    foreign_file: int

    def prepare_home(self) -> None:
        (self.home / "seed").write_text("original")
        (self.home / "directory").mkdir(exist_ok=True)
        (self.home / "escaped").unlink(missing_ok=True)

    def foreign_snapshot(self) -> dict[str, bytes | None]:
        return {path.name: path.read_bytes() if path.is_file() else None for path in self.foreign.iterdir()}

    def write(self, path: str, descriptor: int | None = None, opener: Callable[..., int] | None = None) -> None:
        file = (opener or os.open)(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600, dir_fd=descriptor)
        try:
            os.write(file, b"owned-fixture")
        finally:
            os.close(file)


@contextlib.contextmanager
def descriptor_fixture(scratch: Path) -> Generator[DescriptorFixture]:
    """Close every file/directory fd before removing the private surrogate tree."""
    with descriptor_directories(scratch) as (home, foreign, own_fd, foreign_fd):
        (home / "nested").mkdir()
        for root in (home, foreign):
            (root / "seed").write_text("original")
            (root / "fdfile").write_text("original")
            (root / "directory").mkdir()
        with file_descriptor(home / "fdfile", os.O_RDWR) as own_file, file_descriptor(foreign / "fdfile", os.O_RDWR) as foreign_file:
            yield DescriptorFixture(home, foreign, own_fd, foreign_fd, own_file, foreign_file)


def _close_home_logs(home: Path) -> None:
    """Close only file handlers created beneath this owned temporary home."""
    loggers = [logging.getLogger(), *(item for item in logging.Logger.manager.loggerDict.values() if isinstance(item, logging.Logger))]
    for logger in loggers:
        for handler in list(logger.handlers):
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve().is_relative_to(home):
                logger.removeHandler(handler)
                handler.close()


def _protected_roots(root: Path) -> tuple[Path, ...]:
    return tuple(path.resolve() for path in (*(_HOST_HOME / name for name in (".ananta", ".codex", ".claude")), root / "profile", root / ".claude"))


def _check_scratch(root: Path, scratch: Path) -> None:
    if any(scratch.resolve().is_relative_to(path) for path in _protected_roots(root)):
        raise ValueError("census scratch must be outside protected host/profile directories")


@contextlib.contextmanager
def smoke_scratch(root: Path) -> Generator[Path]:
    """Own smoke scratch in the lane, even when the runner's TMPDIR is protected.

    Only parents created here are candidates for empty-directory cleanup.
    Concurrent contents retain their parent with an advisory; our private
    TemporaryDirectory still has its own unconditional cleanup contract.
    """
    parent = root / "workbench" / ".scratch"
    _check_scratch(root, parent)
    created: list[Path] = []
    candidate = parent
    while not candidate.exists():
        created.append(candidate)
        candidate = candidate.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="fast-return-census-smoke-", dir=parent) as directory:
            yield Path(directory).resolve()
    finally:
        for directory_path in created:
            try:
                directory_path.rmdir()
            except OSError as error:
                print(f"ADVISORY census smoke retains its nonempty/unavailable scratch parent {directory_path}: {error}", file=sys.stderr)
