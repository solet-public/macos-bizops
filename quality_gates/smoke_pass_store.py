"""Owner-controlled append-only smoke observations with per-key flock and CAS.

Each event is an exclusively created, fsynced file. Current projections are
derived from history, so a crash between an event and a projection cannot
resurrect an obsolete PASS. Corrections change optional links, never results.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quality_gates.smoke_identity import canonical, digest

_KEY = re.compile(r"[0-9a-f]{64}\Z")
_METADATA = frozenset({"change_id", "unit_id", "annotation"})


class StaleProofVersionError(ValueError):
    """The reader must use this current version before a new write."""

    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"stale proof version; current_version={current_version}")


def default_store_root() -> Path:
    """A separate namespace under the existing host-external battery store."""
    configured = os.environ.get("RUN_SMOKES_BATTERY_RECEIPT_DIR")
    parent = Path(configured) if configured else Path.home() / ".ananta" / "runtime" / "gate_battery_receipts"
    return parent / "smoke-proofs-v1"


def clean_observation(observation: Mapping[str, Any]) -> bool:
    """Only a complete, single-attempt, fully identified clean PASS is reusable."""
    return (
        observation.get("verdict") == "passed"
        and observation.get("exit_code") == 0
        and observation.get("complete") is True
        and observation.get("identity_complete") is True
        and observation.get("attempt_count") == 1
        and not any(observation.get(key) for key in ("retried", "skipped", "timed_out", "known_failure", "interrupted", "partial"))
    )


def _validate_key(key: str) -> str:
    if not _KEY.fullmatch(key):
        raise ValueError("proof key must be a canonical SHA-256")
    return key


def _project(key: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    current: dict[str, Any] = {"key": key, "version": 0, "observation": None, "observation_event_id": None, "active_attempt": None, "metadata": {}, "retired": False, "reusable": False}
    for event in events:
        current["version"] = event["version"]
        operation, payload = event["operation"], event["payload"]
        if operation == "observe":
            current.update(observation=payload, observation_event_id=event["event_id"], reusable=clean_observation(payload), retired=False, active_attempt=None)
        elif operation == "begin":
            current.update(active_attempt=event["event_id"], reusable=False)
        elif operation == "correct":
            current["metadata"].update(payload["metadata"])
        elif operation == "clear":
            for field in payload["fields"]:
                current["metadata"].pop(field, None)
            if payload["clear_reuse"]:
                current["reusable"] = False
        elif operation == "retire":
            current.update(retired=True, reusable=False)
        elif operation == "reinstate":
            current.update(retired=False, reusable=_can_reinstate(current))
    return current


def _can_reinstate(current: Mapping[str, Any]) -> bool:
    return current["active_attempt"] is None and clean_observation(current["observation"] or {})


def _check_store_path(path: Path) -> None:
    if path.is_symlink() or path.exists() and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022):
        raise PermissionError("proof storage must be owned by this uid without group/world write access")


class SmokePassStore:
    """Current/as-of reads plus immutable observation and metadata lifecycle."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_store_root()

    def _directory(self, key: str) -> Path:
        return self.root / _validate_key(key)

    def _authorize(self, principal: str) -> None:
        if not principal or principal == "unknown":
            raise PermissionError("an explicit authenticated proof principal is required")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _check_store_path(self.root)

    @contextmanager
    def _locked(self, key: str, principal: str) -> Generator[Path]:
        self._authorize(principal)
        directory = self._directory(key)
        directory.mkdir(mode=0o700, exist_ok=True)
        _check_store_path(directory)
        descriptor = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield directory
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def history(self, key: str, *, as_of: int | None = None) -> list[dict[str, Any]]:
        """Read committed events only; every ordinal and event digest is checked."""
        directory = self._directory(key)
        _check_store_path(self.root)
        _check_store_path(directory)
        events: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            event = _read_event(path, len(events) + 1)
            if as_of is not None and event["version"] > as_of:
                break
            events.append(event)
        return events

    def get(self, key: str, *, as_of: int | None = None) -> dict[str, Any]:
        return _project(key, self.history(key, as_of=as_of))

    def list(self) -> list[dict[str, Any]]:
        return [self.get(path.name) for path in sorted(self.root.glob("*")) if _KEY.fullmatch(path.name)]

    def _append(self, key: str, operation: str, payload: Mapping[str, Any], *, expected_version: int, principal: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("proof write requires a reason")
        with self._locked(key, principal) as directory:
            current = self.get(key)
            if current["version"] != expected_version:
                raise StaleProofVersionError(current["version"])
            event = {"key": key, "version": expected_version + 1, "operation": operation, "payload": dict(payload), "principal": principal, "reason": reason, "at": datetime.now(UTC).isoformat()}
            event["event_id"] = digest(event)
            path = directory / f"{expected_version + 1:012d}.json"
            _publish_event(path, event)
            return self.get(key)

    def observe(self, key: str, observation: Mapping[str, Any], *, expected_version: int, principal: str) -> dict[str, Any]:
        """Create a measured attempt; prior REDs remain immutable in history."""
        if observation.get("key") != key or not observation.get("smoke"):
            raise ValueError("observation identity must match its proof key")
        return self._append(key, "observe", observation, expected_version=expected_version, principal=principal, reason="measured smoke attempt")

    def begin(self, key: str, identity: Mapping[str, Any], *, expected_version: int, principal: str) -> dict[str, Any]:
        """Durably mark an attempt before any test runs; never implies PASS."""
        return self._append(key, "begin", identity, expected_version=expected_version, principal=principal, reason="smoke execution starts")

    def retain_attempt(self, observation: Mapping[str, Any], *, principal: str) -> str:
        """Retain original output even if concurrent metadata makes CAS stale."""
        self._authorize(principal)
        directory = self.root / "attempts"
        directory.mkdir(mode=0o700, exist_ok=True)
        _check_store_path(directory)
        path = directory / (digest(observation) + ".json")
        if not path.exists():
            try:
                _publish_event(path, observation)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != canonical(observation):
                    raise ValueError("retained attempt identity collision") from None
        return str(path)

    def correct(self, key: str, metadata: Mapping[str, Any], *, expected_version: int, principal: str, reason: str) -> dict[str, Any]:
        if not metadata.keys() <= _METADATA or any(value is None for value in metadata.values()):
            raise ValueError("only optional change_id/unit_id/annotation may be set or corrected; use clear for None")
        target = self.get(key)["observation_event_id"]
        if target is None:
            raise ValueError("correct requires an existing measured observation")
        return self._append(key, "correct", {"metadata": dict(metadata), "target": target}, expected_version=expected_version, principal=principal, reason=reason)

    def clear(self, key: str, fields: list[str], *, clear_reuse: bool, expected_version: int, principal: str, reason: str) -> dict[str, Any]:
        if not set(fields) <= _METADATA:
            raise ValueError("immutable observation identity/results cannot be cleared")
        return self._append(key, "clear", {"fields": fields, "clear_reuse": clear_reuse}, expected_version=expected_version, principal=principal, reason=reason)

    def retire(self, key: str, *, expected_version: int, principal: str, reason: str) -> dict[str, Any]:
        return self._append(key, "retire", {}, expected_version=expected_version, principal=principal, reason=reason)

    def reinstate(self, key: str, *, current_key: str, expected_version: int, principal: str, reason: str) -> dict[str, Any]:
        current = self.get(key)
        if key != current_key or current["active_attempt"] is not None or not clean_observation(current["observation"] or {}):
            raise ValueError("reinstate requires the identical revalidated clean measured key")
        return self._append(key, "reinstate", {}, expected_version=expected_version, principal=principal, reason=reason)


def _read_event(path: Path, version: int) -> dict[str, Any]:
    _check_store_path(path)
    event = json.loads(path.read_bytes())
    identity = {name: value for name, value in event.items() if name != "event_id"}
    if event["version"] != version or event["event_id"] != digest(identity):
        raise ValueError("corrupt proof history")
    return event


def _publish_event(path: Path, event: Mapping[str, Any]) -> None:
    """Publish complete bytes atomically without overwriting an earlier event."""
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical(event))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink()
