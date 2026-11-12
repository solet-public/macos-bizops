"""Recovery types: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from custody_store import Payload

VERSION = "recovery/v1"


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


_DAY = 86400.0


_EVENTS = frozenset({"PreToolUse", "PostToolUse", "Stop", "SessionEnd", "SessionStart"})


_ZERO = re.compile(r"^0+$")


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


Json = dict[str, Any]


class RecoveryError(RuntimeError):
    """An explicit advisory or integrity failure, never a hook refusal."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class HookEvent:
    """Neutral adapter input; payload is optional for direct payload callers."""

    event: str = ""
    session_id: str = ""
    turn_id: str = ""
    cwd: Path | str = ""
    command: str | Sequence[str] = ""
    edited_paths: tuple[str, ...] = ()
    tool: str = "codex"
    tool_name: str = ""
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class HookResult:
    """Only optional informational text; adapters own their wire rendering."""

    notice: str = ""
    context: str = ""
    saves: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Limits:
    """Local, configurable starting budgets; failures disclose default use."""

    cap_bytes: int = 512 << 20
    cap_files: int = 50000
    cap_file_size: int = 64 << 20
    hard_seconds: float = 8.0
    publication_seconds: float = 1.0
    untracked_seconds: float = 2.0
    store_bytes: int = 5 << 30
    disk_floor: int = 10 << 30
    grace_seconds: float = 30 * _DAY
    trash_seconds: float = _DAY


def command_text(command: str | Sequence[str]) -> str:
    return command if isinstance(command, str) else shlex.join(command)


@dataclass(slots=True)
class CaptureContent:
    """Ephemeral publication accounting; discarded with its one capture."""

    payloads: dict[str, Payload] = field(default_factory=dict)
    new_bytes: int = 0
    seen: set[tuple[str, str, int, str]] = field(default_factory=set)
    stopped: str = ""
    objects: dict[str, Json] = field(default_factory=dict)
