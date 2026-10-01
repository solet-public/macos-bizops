"""Whole-population Git queries for the release-code collector.

Selecting and attesting a release asks Git one question per file: is this path
ignored, and does its HEAD blob equal the artifact's bytes.  A checkout with
tens of thousands of files turned those questions into tens of thousands of
process spawns that held the solet's serial dispatch loop for over an hour
(iss_f694ccef).  The two helpers here keep every answer the same and put the
whole population behind one process each.
"""

from __future__ import annotations

import hashlib
import os
import select
import subprocess
import tempfile
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Final, Self

GIT_TIMEOUT_SECONDS: Final[float] = 10.0
# One whole-population stream (every blob of the release) is bounded as a unit,
# not per file, so it gets its own larger bound than a single git question.
GIT_BULK_TIMEOUT_SECONDS: Final[float] = 600.0
_RECORD_FIELDS: Final[int] = 4
_BLOB_READ_BYTES: Final[int] = 1024 * 1024


class GitBatchError(RuntimeError):
    """A batched Git query could not produce a trustworthy answer."""


class GitIgnoreSession:
    """One long-lived ``git check-ignore`` answering every ignore question.

    ``git check-ignore --stdin -z -v -n`` writes one four-field record per
    path asked, matching or not, and flushes it per path, so a single process
    can be asked about each directory and file in the walk's own order.  The
    answer for a path is the same one ``git check-ignore -q -- <path>`` gives:
    tracked paths never match, and a negated pattern (``!keep.pyc``) is not an
    ignore.  The collector therefore keeps its depth-first order and every
    refusal, while the per-file process spawn is gone.
    """

    def __init__(self, source_root: Path) -> None:
        self._stderr = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(
                ["git", "-C", str(source_root), "check-ignore", "--stdin", "-z", "-v", "-n"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
            )
        except OSError as exc:
            self._stderr.close()
            raise GitBatchError(f"git check-ignore failed to start: {exc}") from exc
        self._buffer = b""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """End the process; it exits when its input closes."""
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass
        try:
            self._process.wait(timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        if self._process.stdout is not None:
            self._process.stdout.close()
        self._stderr.close()

    def is_ignored(self, relative_path: str) -> bool:
        """Return whether Git excludes this untracked path from source identity."""
        asked = os.fsencode(relative_path)
        stdin = self._process.stdin
        if stdin is None:
            raise GitBatchError("git check-ignore has no input pipe")
        try:
            stdin.write(asked + b"\0")
            stdin.flush()
        except OSError as exc:
            raise self._failure(relative_path, str(exc)) from exc
        _source, _line, pattern, answered = self._read_record(relative_path)
        if answered != asked:
            raise self._failure(
                relative_path, f"answered for {os.fsdecode(answered)!r} out of order"
            )
        return bool(pattern) and not pattern.startswith(b"!")

    def _read_record(self, relative_path: str) -> list[bytes]:
        stdout = self._process.stdout
        if stdout is None:
            raise GitBatchError("git check-ignore has no output pipe")
        descriptor = stdout.fileno()
        while self._buffer.count(b"\0") < _RECORD_FIELDS:
            ready, _, _ = select.select([descriptor], [], [], GIT_TIMEOUT_SECONDS)
            if not ready:
                raise self._failure(relative_path, f"no answer within {GIT_TIMEOUT_SECONDS}s")
            chunk = os.read(descriptor, 65536)
            if not chunk:
                self._process.wait()
                raise self._failure(relative_path, self._stderr_text())
            self._buffer += chunk
        fields = self._buffer.split(b"\0", _RECORD_FIELDS)
        self._buffer = fields[_RECORD_FIELDS]
        return fields[:_RECORD_FIELDS]

    def _stderr_text(self) -> str:
        self._stderr.seek(0)
        return self._stderr.read().decode(errors="replace").strip()

    @staticmethod
    def _failure(relative_path: str, detail: str) -> GitBatchError:
        return GitBatchError(f"git ignore check failed for {relative_path}: {detail}")


def blob_digests(source_root: Path, object_ids: set[str]) -> dict[str, str]:
    """SHA-256 every requested blob through one ``git cat-file --batch``.

    An object Git cannot produce is left out of the result, which the caller
    reports as an unavailable HEAD blob, exactly as a failed ``git show`` did.
    Input and output go through temporary files so a release-sized stream
    cannot deadlock a pipe or sit in memory.
    """
    if not object_ids:
        return {}
    requested = sorted(object_ids)
    with tempfile.TemporaryFile() as requests, tempfile.TemporaryFile() as answers:
        requests.write("".join(f"{object_id}\n" for object_id in requested).encode("ascii"))
        requests.seek(0)
        try:
            result = subprocess.run(
                ["git", "-C", str(source_root), "cat-file", "--batch"],
                stdin=requests,
                stdout=answers,
                stderr=subprocess.PIPE,
                check=False,
                timeout=GIT_BULK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitBatchError(f"git cat-file --batch failed: {exc}") from exc
        if result.returncode != 0:
            raise GitBatchError(
                f"git cat-file --batch exited {result.returncode}: "
                f"{result.stderr.decode(errors='replace').strip()}"
            )
        answers.seek(0)
        return _parse_digests(answers, requested)


def _parse_digests(answers: BinaryIO, requested: list[str]) -> dict[str, str]:
    digests: dict[str, str] = {}
    for object_id in requested:
        header = answers.readline().split()
        if not header or header[0].decode("ascii", errors="replace") != object_id:
            raise GitBatchError(f"git cat-file --batch answered out of order near {object_id}")
        if header[1:] == [b"missing"]:
            continue
        if len(header) != 3 or header[1] != b"blob":
            raise GitBatchError(f"git cat-file --batch returned an unexpected header for {object_id}")
        digests[object_id] = _hash_blob(answers, int(header[2]), object_id)
    return digests


def _hash_blob(answers: BinaryIO, size: int, object_id: str) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = answers.read(min(remaining, _BLOB_READ_BYTES))
        if not block:
            raise GitBatchError(f"git cat-file --batch output ended inside {object_id}")
        digest.update(block)
        remaining -= len(block)
    if answers.read(1) != b"\n":
        raise GitBatchError(f"git cat-file --batch framing broke after {object_id}")
    return digest.hexdigest()
