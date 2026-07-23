"""Shared watch/wake session identity and spool-file mechanics (no MCP).

The `watch` command (delivery half) and the `wake` command (turn-injection
half) coordinate through a per-session spool file: watch appends one JSON
line per message-bearing delivery; wake blocks until the spool grows past
its consumed offset. Both derive the SAME per-session identity from the
launcher-exported environment, so the pairing needs no flags and no ambient
configuration.

Appends and reads are serialized with ``flock`` so wake's
truncate-when-fully-consumed can never race an in-flight append.
"""

from __future__ import annotations

import fcntl
import hashlib
from pathlib import Path
from typing import Final

from ananta.core.runtime.port_manager import get_runtime_dir

# Launcher-exported per-session identity (see hydration TEMPLATE_VARS.md —
# these names are part of the seed contract).
WATCH_SESSION_LABEL_ENV: Final[str] = "HOMUNCULUS_AGENT_SESSION_LABEL"
WATCH_SESSION_ID_ENV: Final[str] = "HOMUNCULUS_AGENT_SESSION_ID"

_DIGEST_LENGTH: Final[int] = 24


def watch_instance_digest(agent_session_id: str) -> str:
    """Deterministic per-session digest shared by watch identity and spool path.

    Keyed on the launcher's stable ``ases-...`` session id so a reconnecting
    watcher REPLACES its binding (never mints a sibling) and the wake hook
    finds the same spool without any handshake.
    """
    return hashlib.sha256(
        agent_session_id.encode("utf-8"),
    ).hexdigest()[:_DIGEST_LENGTH]


def default_spool_path(homunculus_name: str, agent_instance_id: str) -> Path:
    """The per-session spool file, colocated with the bridge port files."""
    runtime_dir = get_runtime_dir(homunculus_name)
    return runtime_dir / f"{homunculus_name}.{agent_instance_id}.spool"


def spool_offset_path(spool: Path) -> Path:
    """Sidecar recording the byte offset the wake hook has consumed."""
    return spool.with_name(spool.name + ".offset")


def spool_lock_path(spool: Path) -> Path:
    """Sidecar the wake hook flocks to stay a per-session singleton."""
    return spool.with_name(spool.name + ".lock")


def spool_append(spool: Path, line: str) -> None:
    """Append one delivery line under an exclusive lock.

    Opened per append (deliveries are rare) so wake's locked
    truncate-after-full-consumption always operates on a settled file.
    """
    with spool.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line + "\n")
        handle.flush()
