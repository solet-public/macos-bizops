"""Bounded semantic evidence shared by bridge processes; never retain prose."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

Event = Literal["lifecycle_start", "start", "open", "identity_bound", "register_failed", "open_failed", "failed_poll", "reconnect_poll", "reconnect_tool", "reconnected", "close", "close_failed", "lifecycle_unknown"]
Reason = Literal["explicit_close", "streamable_close", "streamable_shutdown", "idle_timeout"]
ClientReason = Literal["stdio_eof", "stdio_error", "client_shutdown"]
Category = Literal["http", "timeout", "connection", "other"]
_EVENTS = frozenset(get_args(Event))
_REASONS = frozenset(get_args(Reason))
_CLIENT_REASONS = frozenset(get_args(ClientReason))
_CATEGORIES = frozenset(get_args(Category))
MAX_FILE_BYTES = 1024 * 1024
MAX_LINE_BYTES = 2048
_ADVISORY = "[solet-bridge] diagnostic evidence unavailable; record dropped\n"
_SAFE = re.compile(r"[A-Za-z0-9._-]{1,128}\Z", re.ASCII)


def _identifier(value: str | None) -> str | None:
    return value if value is not None and _SAFE.fullmatch(value) else None


def _number(value: int | None) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _advisory() -> None:
    try:
        stream = sys.__stderr__
        if stream is not None:
            stream.write(_ADVISORY)
            stream.flush()
    except (OSError, AttributeError, ValueError):
        pass  # The original host stderr can itself be gone; never recurse.


class BridgeDiagnostics:
    """Append under a nonblocking process lock; rotate only fixed owned files.

    The directory is injectable only for fixtures. Every production writer uses
    the same directory, lock and cap, independent of identity or solet name.
    """

    def __init__(self, solet_name: str, *, directory: Path | None = None) -> None:
        self._solet_name = _identifier(solet_name)
        self._directory = directory if directory is not None else Path.home() / ".ananta/runtime/bridge-diagnostics"

    def emit(
        self,
        event: Event,
        *,
        source: Literal["client", "server"],
        server_lifecycle_id: str | None = None,
        bridge_id: str | None = None,
        agent_instance_id: str | None = None,
        generation: int | None = None,
        request_bridge_id: str | None = None,
        request_generation: int | None = None,
        request_lifecycle_id: str | None = None,
        reason: Reason | None = None,
        client_reported_reason: str | None = None,
        category: Category | None = None,
        status: int | None = None,
        count: int | None = None,
        threshold: int | None = None,
        attempt: int | None = None,
    ) -> None:
        """Serialize only known semantic fields, never exceptions or payloads."""
        row = {
            "schema_version": 1,
            "timestamp": datetime.now(UTC).isoformat(),
            "source": source if source in {"client", "server"} else None,
            "solet_name": self._solet_name,
            "pid": os.getpid(),
            "event": event if event in _EVENTS else None,
            "server_lifecycle_id": _identifier(server_lifecycle_id),
            "bridge_id": _identifier(bridge_id),
            "agent_instance_id": _identifier(agent_instance_id),
            "generation": _number(generation),
            "request_bridge_id": _identifier(request_bridge_id),
            "request_generation": _number(request_generation),
            "request_lifecycle_id": _identifier(request_lifecycle_id),
            "reason": reason if reason in _REASONS else None,
            "client_reported_reason": client_reported_reason if client_reported_reason in _CLIENT_REASONS else None,
            "category": category if category in _CATEGORIES else None,
            "status": _number(status),
            "count": _number(count),
            "threshold": _number(threshold),
            "attempt": _number(attempt),
        }
        encoded = (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(encoded) > MAX_LINE_BYTES:
            _advisory()
            return
        try:
            self._append(encoded)
        except OSError:
            _advisory()

    def _append(self, encoded: bytes) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self._directory / "evidence.lock").open("a", encoding="ascii") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                current = self._directory / "evidence.jsonl"
                if current.exists() and current.stat().st_size + len(encoded) > MAX_FILE_BYTES:
                    self._rotate(current)
                # Reopen after taking the lock; no descriptor outlives rotation.
                with current.open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _rotate(self, current: Path) -> None:
        for index in (3, 2, 1):
            target = self._directory / f"evidence.jsonl.{index}"
            source = current if index == 1 else self._directory / f"evidence.jsonl.{index - 1}"
            if source.exists():
                source.replace(target)
