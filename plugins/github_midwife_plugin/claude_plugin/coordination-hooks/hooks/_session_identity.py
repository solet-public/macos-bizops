#!/usr/bin/env python3
"""Durable fleet identity for an env-less Claude Code session (read-only).

A session started outside the fleet launchers (the Claude desktop app, or a
bare ``claude`` in a terminal) exports none of the launcher identity names, so
every coordination hook used to treat it as "not a fleet session" and exit.
Claude Code itself exports ``CLAUDE_CODE_SESSION_ID`` to every child it starts
(hooks, the Bash tool and MCP servers), and a resume keeps that id. The
session's MCP bridge or its bridge CLI persists ONE identity record per Claude
session id in the solet runtime directory. This module reads that record (or,
before either has run, mints the very same values from the session id) and
overlays it onto this hook process's own environment, so the hook and any
``solet-bridge`` child it starts carry the same instance id, session id and
label. It never writes a file.

Env wins: when any launcher identity name is present nothing is read or
overlaid, so a launcher-started session behaves exactly as before. The overlay
also needs the solet's coordination receipt at the clone convention
``<root>/profile/data/coordination-hooks/claude/installation.v1.json`` (root:
``CLAUDE_PROJECT_DIR``, else the checkout holding ``.claude/hooks/``); without
it the hook stays inert, as before.

``agent_messaging_plugin.env_contract`` is the venv-side twin that writes the
record; ``desktop_session_identity_smoke.py`` pins the two to the same values.
Stdlib-only: hooks run outside the venv.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

CLAUDE_SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"
SCHEMA = "solet_claude_session_identity_v1"
DIRNAME = "claude-session-identity"
AGENT_ID = "claude_code"
RECEIPT_RELATIVE = Path("profile/data/coordination-hooks/claude/installation.v1.json")
CLAIM_PATH = "solet-bridge watch --role <Name> (run it in the background; the CLI alone claims the role)"
_LAUNCHER_NAMES = ("AGENT_INSTANCE_ID", "AGENT_SESSION_ID", "AGENT_SESSION_LABEL")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_FIELDS = ("agent_instance_id", "agent_session_id", "session_label")


@dataclass(frozen=True)
class SessionIdentity:
    agent_instance_id: str
    agent_session_id: str
    session_label: str
    path: Path


def runtime_dir(environ: MutableMapping[str, str]) -> Path:
    """``port_manager.get_runtime_dir``'s rule, without the venv import."""
    xdg = environ.get("XDG_RUNTIME_DIR", "")
    if xdg:
        return Path(xdg) / "ananta"
    return Path(environ.get("HOME") or Path.home()) / ".ananta" / "runtime"


def minted(claude_session_id: str) -> dict[str, str]:
    """The identity values every writer derives from the Claude session id."""
    digest = hashlib.sha256(f"solet-claude-session:{claude_session_id}".encode()).hexdigest()
    return {
        "agent_instance_id": f"agi-{digest[:32]}",
        "agent_session_id": f"ases-claude-{claude_session_id}",
        "session_label": f"claude-{claude_session_id[:8]}",
    }


def _read(path: Path, claude_session_id: str) -> dict[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("claude_session_id") != claude_session_id:
        return None
    fields = {name: value.get(name) for name in _FIELDS}
    if not all(isinstance(item, str) and item.strip() for item in fields.values()):
        return None
    return {name: str(item) for name, item in fields.items()}


def _receipt(environ: MutableMapping[str, str], hook_file: str) -> tuple[Path, Path, str] | None:
    """``(receipt, root, solet_name)`` from the first clone carrying a receipt."""
    roots: list[Path] = []
    if environ.get("CLAUDE_PROJECT_DIR"):
        roots.append(Path(environ["CLAUDE_PROJECT_DIR"]))
    here = Path(hook_file).resolve().parent
    if here.name == "hooks" and here.parent.name == ".claude":
        roots.append(here.parent.parent)
    for root in roots:
        receipt = root / RECEIPT_RELATIVE
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        solet = value.get("solet_name") if isinstance(value, dict) else None
        if isinstance(solet, str) and solet:
            return receipt, root, solet
    return None


def resolve(environ: MutableMapping[str, str], *, payload_session_id: str = "") -> SessionIdentity | None:
    """The record's identity (minted values while absent); ``None`` when env governs."""
    if any(environ.get(name, "").strip() for name in _LAUNCHER_NAMES):
        return None
    claude_session_id = environ.get(CLAUDE_SESSION_ID_ENV, "").strip() or payload_session_id.strip()
    if _SESSION_ID.fullmatch(claude_session_id) is None:
        return None
    path = runtime_dir(environ) / DIRNAME / f"{claude_session_id}.json"
    stored = _read(path, claude_session_id) or minted(claude_session_id)
    return SessionIdentity(stored["agent_instance_id"], stored["agent_session_id"], stored["session_label"], path)


def apply(
    hook_file: str,
    *,
    payload_session_id: str = "",
    environ: MutableMapping[str, str] | None = None,
) -> SessionIdentity | None:
    """Overlay the session's identity onto this hook's env (setdefault only)."""
    env = os.environ if environ is None else environ
    if any(env.get(name, "").strip() for name in _LAUNCHER_NAMES):
        return None
    located = _receipt(env, hook_file)
    if located is None:
        return None
    receipt, root, solet_name = located
    identity = resolve(env, payload_session_id=payload_session_id)
    if identity is None:
        return None
    overlay = {
        "SOLET_NAME": solet_name,
        "AGENT_IDENTITY": AGENT_ID,
        "AGENT_INSTANCE_ID": identity.agent_instance_id,
        "AGENT_SESSION_ID": identity.agent_session_id,
        "AGENT_SESSION_LABEL": identity.session_label,
        "AGENT_COORDINATION_RECEIPT_PATH": str(receipt),
        "ANANTA_SESSION_MAPPING_SPOOL_DIR": str(root / "profile" / "data" / "session_claude_mapping_spool"),
    }
    cli = root / ".venv" / "bin" / "solet-bridge"
    if cli.is_file():
        overlay["AGENT_WAKE_CLI"] = str(cli)
        overlay["SOLET_BRIDGE"] = str(cli)
    for name, value in overlay.items():
        env.setdefault(name, value)
    return identity
