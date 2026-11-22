"""Recovery git: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from recovery_types import Json, RecoveryError


@dataclass(frozen=True, slots=True)
class GitResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    objects: Mapping[str, bytes] = field(default_factory=dict)
    timed_out: bool = False


def _read_exact(stream: IO[bytes], length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(min(remaining, 1 << 20))
        if not chunk:
            raise RecoveryError("child_short_answer")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _environment(overrides: Mapping[str, str] | None) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env.update(overrides or {})
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_NO_LAZY_FETCH": "1"})
    return env


def _spawn(folder: Path, arguments: Sequence[str], deadline: float, *, request: bytes | None = None, objects: Mapping[str, int] | None = None, environment: Mapping[str, str] | None = None) -> GitResult:
    """The one child boundary; both exchange shapes share a group watchdog."""
    if time.monotonic() >= deadline:
        return GitResult(-1, timed_out=True)
    stream = objects is not None
    stdin = subprocess.PIPE if request is not None or stream else subprocess.DEVNULL
    try:
        child = subprocess.Popen(["git", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", *arguments], cwd=folder, env=_environment(environment), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL if stream else subprocess.PIPE, close_fds=True, start_new_session=True)
    except FileNotFoundError as exc:
        raise RecoveryError("git_missing") from exc
    expired = threading.Event()
    answers: dict[str, bytes] = {}

    def kill(mark_expired: bool = True) -> None:
        if mark_expired:
            expired.set()
        if child.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)

    def bounded() -> GitResult:
        try:
            output, error = child.communicate(input=request, timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            kill()
            output, error = child.communicate()
        return GitResult(child.returncode, output, error or b"", timed_out=expired.is_set())

    def lockstep() -> GitResult:
        assert child.stdin is not None and child.stdout is not None
        for oid, size in (objects or {}).items():
            if expired.is_set():
                break
            child.stdin.write(oid.encode("ascii") + b"\n")
            child.stdin.flush()
            header = child.stdout.readline().rstrip(b"\n").split()
            if header == [oid.encode(), b"missing"]:
                continue
            if header != [oid.encode(), b"blob", str(size).encode()]:
                raise RecoveryError("child_short_answer", "cat-file header")
            data = _read_exact(child.stdout, size + 1)
            if data[-1:] != b"\n":
                raise RecoveryError("child_short_answer", "cat-file terminator")
            answers[oid] = data[:-1]
        child.stdin.close()
        child.wait(timeout=max(0.001, deadline - time.monotonic()))
        return GitResult(child.returncode, objects=answers, timed_out=expired.is_set())

    def cleanup() -> None:
        if child.poll() is None:
            kill(False)
            child.wait()
        for descriptor in (child.stdin, child.stdout, child.stderr):
            if descriptor is not None:
                descriptor.close()

    watchdog = threading.Timer(max(0.001, deadline - time.monotonic()), kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        return lockstep() if stream else bounded()
    except (BrokenPipeError, RecoveryError, subprocess.TimeoutExpired) as exc:
        kill(isinstance(exc, subprocess.TimeoutExpired))
        child.wait()
        return GitResult(-1, objects=answers, timed_out=expired.is_set())
    finally:
        watchdog.cancel()
        cleanup()


class Git:
    """One capture/restore's shared deadline and its only Git access path."""

    def __init__(self, folder: Path, deadline: float, environment: Mapping[str, str] | None = None) -> None:
        self.folder, self.deadline = folder, deadline
        self.environment = environment

    def result(self, arguments: Sequence[str], *, request: bytes | None = None) -> GitResult:
        return _spawn(self.folder, arguments, self.deadline, request=request, environment=self.environment)

    def read(self, arguments: Sequence[str], *, request: bytes | None = None, allow: tuple[int, ...] = (0,)) -> bytes:
        result = self.result(arguments, request=request)
        if result.timed_out:
            raise RecoveryError("deadline")
        if result.returncode not in allow:
            raise RecoveryError("git_read_failed", arguments[0])
        return result.stdout

    def text(self, arguments: Sequence[str], *, allow: tuple[int, ...] = (0,)) -> str:
        return os.fsdecode(self.read(arguments, allow=allow)).strip()

    def blobs(self, sizes: Mapping[str, int]) -> GitResult:
        return _spawn(self.folder, ["cat-file", "--batch"], self.deadline, objects=sizes, environment=self.environment)


def read_tree(git: Git, reference: str) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    if not reference:
        return result
    raw = git.read(["ls-tree", "-r", "-z", reference])
    for item in raw.split(b"\0"):
        if item:
            metadata, path = item.split(b"\t", 1)
            mode, _kind, oid = metadata.split()
            result[os.fsdecode(path)] = (int(mode, 8), oid.decode("ascii"))
    return result


def read_index(git: Git) -> dict[tuple[str, int], tuple[int, str]]:
    result: dict[tuple[str, int], tuple[int, str]] = {}
    for item in git.read(["ls-files", "--stage", "-z"]).split(b"\0"):
        if item:
            metadata, path = item.split(b"\t", 1)
            mode, oid, stage = metadata.split()
            result[(os.fsdecode(path), int(stage))] = (int(mode, 8), oid.decode("ascii"))
    return result


def changed_paths(git: Git, arguments: Sequence[str]) -> list[str]:
    fields = git.read(arguments).split(b"\0")
    return [os.fsdecode(fields[index + 1]) for index in range(0, len(fields) - 1, 2) if fields[index].startswith(b":")]


def object_sizes(git: Git, ids: Sequence[str]) -> dict[str, int]:
    if not ids:
        return {}
    request = b"".join(oid.encode("ascii") + b"\n" for oid in ids)
    result = git.read(["cat-file", "--batch-check"], request=request).splitlines()
    if len(result) != len(ids):
        raise RecoveryError("child_short_answer", "cat-file size count")
    sizes: dict[str, int] = {}
    for oid, answer in zip(ids, result, strict=True):
        fields = answer.split()
        if fields[:1] != [oid.encode("ascii")]:
            raise RecoveryError("child_short_answer", "cat-file size identity")
        if len(fields) == 3 and fields[1] == b"blob":
            sizes[oid] = int(fields[2])
    return sizes


def unquote_path(raw: bytes) -> str:
    if not raw.startswith(b'"'):
        return os.fsdecode(raw)
    body, output, index = raw[1:-1], bytearray(), 0
    escapes = {ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10, ord("v"): 11, ord("f"): 12, ord("r"): 13}
    while index < len(body):
        value = body[index]
        index += 1
        if value == 92 and index < len(body):
            value = body[index]
            index += 1
            if 48 <= value <= 55:
                digits = bytes([value])
                while len(digits) < 3 and index < len(body) and 48 <= body[index] <= 55:
                    digits += body[index : index + 1]
                    index += 1
                value = int(digits, 8)
            else:
                value = escapes.get(value, value)
        output.append(value)
    return os.fsdecode(bytes(output))


def ignored_paths(git: Git, paths: Sequence[str], *, globals_: Sequence[str] = ("check-ignore",)) -> dict[str, bool | None]:
    request = b"".join(os.fsencode(path) + b"\0" for path in paths)
    result = git.result([*globals_, "--stdin", "-z", "-v", "-n"], request=request)
    fields = result.stdout.split(b"\0")
    if result.timed_out or result.returncode not in {0, 1} or fields[-1:] != [b""] or len(fields) != 4 * len(paths) + 1:
        return dict.fromkeys(paths, None)
    answer: dict[str, bool | None] = {}
    for index, path in enumerate(paths):
        source, _line, pattern, returned = fields[index * 4 : index * 4 + 4]
        if returned != os.fsencode(path):
            return dict.fromkeys(paths, None)
        # A negative pattern is an explicit admission, not an ignored match.
        answer[path] = bool(source and pattern and not pattern.startswith(b"!"))
    return answer


def objects_exist(git: Git, ids: Sequence[str]) -> bool:
    result = git.read(["cat-file", "--batch-check"], request=b"".join(oid.encode("ascii") + b"\n" for oid in ids)).splitlines()
    return len(result) == len(ids) and all(len(fields := answer.split()) == 3 and fields[0] == oid.encode("ascii") for oid, answer in zip(ids, result, strict=True))


def index_info(entries: list[Json], object_format: str) -> bytes:
    zero = "0" * (64 if object_format == "sha256" else 40)
    conflicts = sorted({entry["path"] for entry in entries if entry["stage"]})
    clear = b"".join(f"0 {zero}\t".encode("ascii") + os.fsencode(path) + b"\0" for path in conflicts)
    records = b"".join(f"{entry['mode'] or 0:o} {entry['oid'] or zero} {entry['stage']}\t".encode("ascii") + os.fsencode(entry["path"]) + b"\0" for entry in entries)
    return clear + records


def quote_path(path: Path) -> bytes:
    return b'"' + b"".join(bytes([byte]) if 32 <= byte < 127 and byte not in {34, 92} else f"\\{byte:03o}".encode("ascii") for byte in os.fsencode(path)) + b'"'
