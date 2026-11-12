"""Stdlib CLI client for platform work folders; unavailable tracking is advisory.

The register owns lifecycle transitions and the sweeper owns physical removal.
Pending registrations and retained receipts live beside a folder, never in a
worktree. Creator failures must retain their original exception and scratch.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ACTIVE: ContextVar[WorkFolder | None] = ContextVar("platform_work_folder", default=None)
_TIMEOUT = 10


def advisory(message: object) -> None:
    """Report degraded tracking without converting it into a creator failure."""
    print(f"ADVISORY work_folder: {' '.join(str(message).splitlines())}", file=sys.stderr)


def process_binding() -> str:
    """Use the same PID/start/host identity that the register sweeper reads."""
    try:
        result = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "lstart="], capture_output=True, text=True, check=True, timeout=5)
        start = result.stdout.strip()
        if not start:
            raise ValueError("process start time is empty")
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        advisory(f"process start unknown: {error}")
        start = "unknown"
    return "process:" + json.dumps({"pid": os.getpid(), "start_time": start, "host": socket.gethostname()}, sort_keys=True)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), sort_keys=True, default=str) + "\n").encode()


@contextmanager
def _directory(root: Path, *names: str) -> Generator[int]:
    """Open private metadata directories without traversing a substituted link."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in names:
            try:
                os.mkdir(name, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _write_at(directory: int, name: str, raw: bytes) -> None:
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    except FileExistsError:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as existing:
            if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode) or existing.read(len(raw) + 1) != raw:
                raise ValueError("retained work-folder evidence differs from its content address") from None
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


@dataclass
class WorkFolder:
    """One creator's path and versioned CLI custody; no filesystem remover."""

    path: Path
    bound: str
    purpose: str
    harvest: str
    owner: str
    environment: Mapping[str, str] = field(repr=False)
    folder_id: str | None = None

    def _call(self, *arguments: str, write: bool = False) -> dict[str, Any]:
        home = self.environment.get("HOME", str(Path.home()))
        executable = self.environment.get("PROJECT_SOLET_CLI") or str(Path(home) / "Workspace/project-solet/.venv/bin/psolet")
        identity: list[str] = []
        if write:
            for variable, option in (("SOLET_NAME", "solet"), ("AGENT_INSTANCE_ID", "agent-instance-id"), ("AGENT_SESSION_ID", "agent-session-id"), ("AGENT_MODEL", "actor-model"), ("AGENT_EFFORT", "actor-effort")):
                if value := self.environment.get(variable):
                    identity.extend((f"--{option}", value))
        result = subprocess.run([executable, "work-folder", *arguments, *identity], env=dict(self.environment), capture_output=True, text=True, check=False, timeout=_TIMEOUT)
        if result.returncode:
            raise ValueError(f"psolet {arguments[0]} exited {result.returncode}: {result.stderr.strip()[:500]}")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("psolet returned no work-folder object")
        return payload

    def pending(self, **detail: object) -> None:
        """Write the sweeper's declared sidecar schema, outside the work folder."""
        kind, reference = self.bound.split(":", 1)
        payload = dict(schema="work-folder-pending/v1", path=str(self.path), purpose=self.purpose, bound_kind=kind, bound_ref=reference, harvest_kind=self.harvest, session_id=self.owner, creator="ananta.work_folders", **detail)
        name = hashlib.sha256(_json_bytes(payload)).hexdigest() + ".json"
        try:
            with _directory(self.path.parent, ".work_folders", "pending") as directory:
                try:
                    _write_at(directory, name, _json_bytes(payload))
                except FileExistsError:
                    pass
        except (OSError, ValueError) as error:
            advisory(f"pending sidecar unavailable for {self.path}: {error}")

    def _transition(self, action: str, row: Mapping[str, Any], *arguments: str) -> dict[str, Any]:
        return self._call(action, "--folder", str(row["folder_id"]), "--expected-version", str(row["version"]), "--reason", "platform creator lifecycle", *arguments, write=True)

    def register(self) -> None:
        """Register after creation, or reopen the same physical folder on resume."""
        try:
            rows = self._call("list", "--root", str(self.path.parent), "--bound", self.bound)["folders"]
            current = next((row for row in rows if row["path"] == str(self.path) and row["state"] not in ("deleted", "retired")), None)
            if current is not None:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) != (current["dev"], current["ino"]):
                    raise ValueError("registered folder identity changed; retained without mutation")
                if current["state"] in ("due", "harvested"):
                    current = self._transition("reopen", current)
                if current["state"] != "open":
                    raise ValueError(f"registered folder remains {current['state']}; retained")
            else:
                current = self._call("open", "--root", str(self.path.parent), "--path", str(self.path), "--purpose", self.purpose, "--bound", self.bound, "--owner", self.owner, "--harvest", self.harvest, "--expected-version", "0", "--reason", "platform creator allocated folder", write=True)
            self.folder_id = str(current["folder_id"])
            for message in current.get("advisories", []):
                advisory(message)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            advisory(f"registration pending for {self.path}: {error}")
            self.pending()

    def close(self, receipt: Mapping[str, Any] | None = None) -> None:
        """Close on a terminal creator event; retain failed harvests for the sweeper."""
        if self.folder_id is None:
            self.pending(terminal=True, receipt=receipt)
            return
        try:
            row = self._call("show", "--folder", self.folder_id)
            harvest = "regenerable" if receipt is None else str(receipt["kind"])
            if row["harvest_kind"] != harvest:
                row = self._transition("correct", row, "--harvest", harvest)
            if row["state"] == "open":
                row = self._transition("close", row)
            if receipt is not None and row["state"] == "due":
                self._transition("harvest-record", row, "--receipt-json", json.dumps(dict(receipt), sort_keys=True))
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            advisory(f"close/harvest retained {self.path}: {error}")
            self.pending(terminal=True, receipt=receipt)

    def _mark_due(self) -> None:
        """Restore evidence custody before close can complete a regenerable harvest."""
        if self.folder_id is None:
            return
        try:
            row = self._call("show", "--folder", self.folder_id)
            if row["harvest_kind"] != "evidence_copy":
                row = self._transition("correct", row, "--harvest", "evidence_copy")
            if row["state"] == "open":
                self._transition("close", row)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            advisory(f"terminal close retained {self.path}: {error}")


def register_work_folder(path: Path, *, purpose: str, bound: str, harvest: str = "regenerable", owner: str = "", environment: Mapping[str, str] | None = None) -> WorkFolder:
    """Register an external creator's existing path before it writes work bytes."""
    env = dict(os.environ if environment is None else environment)
    folder = WorkFolder(path.resolve(), bound, purpose, harvest, owner or env.get("AGENT_INSTANCE_ID") or f"pid:{os.getpid()}", env)
    folder.register()
    return folder


def current_work_folder() -> Path | None:
    """The release run's root for nested creators, isolated across worker threads."""
    active = _ACTIVE.get()
    return None if active is None else active.path


@contextmanager
def work_folder_scope(folder: WorkFolder) -> Generator[None]:
    """Route nested public acts and assemble policy staging into their run."""
    token = _ACTIVE.set(folder)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _evidence_files(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for entry in sorted(path.rglob("*")):
        relative = entry.relative_to(path)
        if not {"receipts", "evidence"}.intersection(relative.parts):
            continue
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"linked evidence retained in source: {relative}")
        if stat.S_ISREG(info.st_mode):
            files[relative.as_posix()] = entry.read_bytes()
    return files


def finish_release_folder(folder: WorkFolder, receipt: Mapping[str, Any]) -> dict[str, str] | None:
    """Retain failure receipts and named VM custody before marking scratch due.

    The VM disk is outside this lifecycle. Success is regenerable; parked runs
    are never passed here. All tracking or evidence failures leave scratch intact.
    """
    try:
        if receipt.get("state") == "completed":
            folder.close()
            return None
        folder._mark_due()
        files = _evidence_files(folder.path)
        manifest = {"schema": "work-folder-evidence/v1", "bound": folder.bound, "receipt": dict(receipt), "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
        raw = _json_bytes(manifest)
        digest = hashlib.sha256(raw).hexdigest()
        with _directory(folder.path.parent, ".work_folders", "receipts", digest) as directory:
            for content in files.values():
                # Content-addressed evidence leaves preserve every selected receipt.
                leaf = hashlib.sha256(content).hexdigest()
                try:
                    _write_at(directory, leaf, content)
                except FileExistsError:
                    pass
            try:
                _write_at(directory, "work-folder-manifest.json", raw)
            except FileExistsError:
                pass
        pointer = {"kind": "evidence_copy", "path": str(folder.path.parent / ".work_folders/receipts" / digest), "manifest_sha256": digest}
        folder.close(pointer)
        return pointer
    except (OSError, ValueError, TypeError) as error:
        advisory(f"failure evidence retained in {folder.path}: {error}")
        return None
