"""Client for the solet's own work-folder records; unavailable tracking is advisory.

The solet's records own lifecycle transitions and its sweep owns physical
removal: a creator that ends its work asks the solet to remove the folder. A creator that holds the solet's state records (the release run) works
on them in process; every other creator calls the solet bridge, so no creator
ever waits on a bridge that is its own host. The module imports the standard
library only: the solet's own modules load when a state handle is used.
Pending registrations and retained receipts live beside a folder, never in a
worktree. Creator failures must retain their original exception and scratch.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.services.lifecycle_management_service.work_folder_records import WorkFolderStore

_ACTIVE: ContextVar[WorkFolder | None] = ContextVar("platform_work_folder", default=None)
_TIMEOUT = 10
_VERBS = "service_interface::lifecycle_management_service::"
# The creator's identity, as the work-folder verbs name it; recorded for the history, never an authority.
_ACTOR_FIELDS = (("AGENT_INSTANCE_ID", "agent_instance_id"), ("AGENT_SESSION_ID", "agent_session_id"), ("AGENT_MODEL", "actor_model"), ("AGENT_EFFORT", "actor_effort"))


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


def _bridge_executable(environment: Mapping[str, str]) -> str:
    """The solet bridge: SOLET_BRIDGE, then AGENT_WAKE_CLI, then ``solet-bridge`` on PATH."""
    found = environment.get("SOLET_BRIDGE") or environment.get("AGENT_WAKE_CLI") or shutil.which("solet-bridge", path=environment.get("PATH"))
    if not found:
        raise ValueError("solet bridge not found")
    return found


def _verb_data(verb: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The verb's ``data``; a refusal or an incomplete call raises.

    The bridge's outer envelope and a finished job's row both carry ``status`` and
    the verb's own envelope as ``result``, so one decode reads either.
    """
    result = payload.get("result")
    if payload.get("status") != "completed" or not isinstance(result, dict):
        raise ValueError(f"work-folder verb {verb} did not complete: {str(payload)[:500]}")
    if result.get("success") is False or result.get("error") or result.get("action_status", "completed") != "completed":
        raise ValueError(f"work-folder verb {verb} refused: {str(result.get('error') or result)[:500]}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"work-folder verb {verb} returned no data")
    return data


def _in_process(state: object, verb: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Run the verb on the solet's own work-folder records, as the bridge would, without a subprocess."""
    from ananta.error_handling import FrameworkError
    from ananta.llm.agent_messaging.state_results import StateOperationError
    from ananta.services.lifecycle_management_service.work_folder_verbs import WorkFolderVerbs

    class _StateVerbs(WorkFolderVerbs):
        # The verbs reach their store through the orchestrator's state service; host them on the state this creator holds.
        _orchestrator = SimpleNamespace(get_service=lambda _name: state)

    method: Callable[..., dict[str, Any]] = getattr(_StateVerbs(), verb)
    try:
        envelope = method(**arguments)
    except (FrameworkError, StateOperationError) as error:
        raise ValueError(f"work-folder verb {verb} failed: {error}") from error
    return _verb_data(verb, {"status": "completed", "result": envelope})


@dataclass
class WorkFolder:
    """One creator's path and versioned custody of its record; it asks the solet to remove a folder and never removes one itself."""

    path: Path
    bound: str
    purpose: str
    harvest: str
    owner: str
    environment: Mapping[str, str] = field(repr=False)
    folder_id: str | None = None
    state: WorkFolderStore | None = field(default=None, repr=False)

    def _call(self, verb: str, arguments: Mapping[str, Any], *, write: bool = False) -> dict[str, Any]:
        """One work-folder verb: in process when the creator holds the solet's state, else over the solet bridge."""
        sent = dict(arguments)
        if write:
            sent.update({option: value for variable, option in _ACTOR_FIELDS if (value := self.environment.get(variable))})
        if self.state is not None:
            return _in_process(self.state, verb, sent)
        executable = _bridge_executable(self.environment)
        result = subprocess.run([executable, "call", _VERBS + verb, json.dumps(sent)], env=dict(self.environment), capture_output=True, text=True, check=False, timeout=_TIMEOUT)
        if result.returncode:
            raise ValueError(f"work-folder verb {verb} exited {result.returncode}: {result.stderr.strip()[:500]}")
        return _verb_data(verb, json.loads(result.stdout))

    def _show(self) -> dict[str, Any]:
        return dict(self._call("read_work_folders", {"mode": "show", "folder_id": self.folder_id})["folders"][0])

    def _listed(self) -> Iterator[dict[str, Any]]:
        """Every record bound to this creator under its folder's parent, a page at a time.

        A path that was removed and made again leaves its old records behind, so the
        record of a live folder is not always on the first page.
        """
        query: dict[str, Any] = {"mode": "list", "parent_dir": str(self.path.parent), "bound": self.bound}
        while True:
            page = self._call("read_work_folders", query)
            yield from page["folders"]
            if page["next"] is None:
                return
            # The bridge refuses a null for an optional string, so "after" is sent only once a page has given a token.
            query = {**query, "after": page["next"]}

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

    def _transition(self, action: str, row: Mapping[str, Any], **changes: str) -> dict[str, Any]:
        return self._call("transition_work_folder", {"folder_id": str(row["folder_id"]), "action": action, "expected_version": row["version"], "reason": "platform creator lifecycle", **changes}, write=True)

    def register(self) -> None:
        """Register after creation, or reopen the same physical folder on resume."""
        try:
            current = next((row for row in self._listed() if row["path"] == str(self.path) and row["state"] not in ("deleted", "retired")), None)
            if current is not None:
                info = self.path.lstat()
                # The record keeps device and inode as decimal text.
                if (str(info.st_dev), str(info.st_ino)) != (str(current["dev"]), str(current["ino"])):
                    raise ValueError("registered folder identity changed; retained without mutation")
                if current["state"] in ("due", "harvested"):
                    current = self._transition("reopen", current)
                if current["state"] != "open":
                    raise ValueError(f"registered folder remains {current['state']}; retained")
            else:
                current = self._call("open_work_folder", {"path": str(self.path), "purpose": self.purpose, "bound": self.bound, "harvest": self.harvest, "owner": self.owner}, write=True)
            self.folder_id = str(current["folder_id"])
            for notice in current.get("advisories", []):
                advisory(notice["message"])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            advisory(f"registration pending for {self.path}: {error}")
            self.pending()

    def close(self, receipt: Mapping[str, Any] | None = None) -> None:
        """Close on a terminal creator event: without a receipt the solet removes the folder in the same step, with one it keeps it."""
        if self.folder_id is None:
            self.pending(terminal=True, receipt=receipt)
            return
        try:
            row = self._show()
            harvest = "regenerable" if receipt is None else str(receipt["kind"])
            if row["harvest_kind"] != harvest:
                row = self._transition("correct", row, harvest_kind=harvest)
            if row["state"] == "open":
                row = self._transition("close", row)
            if receipt is not None and row["state"] == "due":
                self._transition("harvest_record", row, receipt_json=json.dumps(dict(receipt), sort_keys=True))
            if receipt is None:
                self._remove()
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            advisory(f"close/harvest retained {self.path}: {error}")
            self.pending(terminal=True, receipt=receipt)

    def _remove(self) -> None:
        """Ask the solet to remove this closed folder; a failure is an advisory and the record stays due or harvested."""
        try:
            for notice in self._call("sweep_work_folders", {"apply": True, "folder_id": self.folder_id})["advisories"]:
                advisory(f"{notice['code']}: {notice['message']}")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            advisory(f"removal retained {self.path}: {error}")

    def _mark_due(self) -> None:
        """Restore evidence custody before close can complete a regenerable harvest."""
        if self.folder_id is None:
            return
        try:
            row = self._show()
            if row["harvest_kind"] != "evidence_copy":
                row = self._transition("correct", row, harvest_kind="evidence_copy")
            if row["state"] == "open":
                self._transition("close", row)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            advisory(f"terminal close retained {self.path}: {error}")


def register_work_folder(path: Path, *, purpose: str, bound: str, harvest: str = "regenerable", owner: str = "", environment: Mapping[str, str] | None = None, state: WorkFolderStore | None = None) -> WorkFolder:
    """Register a creator's existing path before it writes work bytes.

    A creator that holds the solet's state records passes them as ``state`` and
    works on them in process; any other creator calls the solet bridge.
    """
    env = dict(os.environ if environment is None else environment)
    folder = WorkFolder(path.resolve(), bound, purpose, harvest, owner or env.get("AGENT_INSTANCE_ID") or f"pid:{os.getpid()}", env, state=state)
    folder.register()
    return folder


def open_release_folder(state: WorkFolderStore, run_id: str, *, environment: Mapping[str, str] | None = None) -> WorkFolder | None:
    """The release run's scratch folder that is still open, as a creator handle, or None when there is none to end.

    A later run ends a refused run's scratch through this: it finds the folder by the run's own binding, so it
    needs no knowledge of the scratch layout. It never raises: a store that cannot answer is an advisory, and the
    folder is kept.
    """
    from ananta.error_handling import FrameworkError
    from ananta.llm.agent_messaging.state_results import StateOperationError
    from ananta.services.lifecycle_management_service import work_folder_records as records

    try:
        rows = records.list_folders(records.require_work_folder_store(state), bound_kind="release_run", bound_ref=run_id, state="open", limit=1).rows
        if not rows or not Path(str(rows[0]["path"])).is_dir():
            return None
        row = rows[0]
        return WorkFolder(Path(str(row["path"])), f"release_run:{run_id}", str(row["purpose"]), str(row["harvest_kind"]), str(row["owner"]), dict(os.environ if environment is None else environment), folder_id=str(row["folder_id"]), state=state)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, FrameworkError, StateOperationError) as error:
        advisory(f"open release folder unavailable for {run_id}: {error}")
        return None


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
    are never passed here, and a refused run is passed here only once a later run
    supersedes it (release_run._end_superseded_scratch). All tracking or evidence
    failures leave scratch intact.
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
