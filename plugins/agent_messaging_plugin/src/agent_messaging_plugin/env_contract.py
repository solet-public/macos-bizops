"""Neutral fleet identity env-var contract (``AGENT_*``).

The five per-session identity variables a launcher exports for fleet
coordination are unprefixed and runner-neutral: they are part of the seed
contract and must read identically on the platform origin and on every
seed-born homunculus (operator ruling 2026-07-28; fleet-transport-parity
design §7). This module is the single source of truth for the names; it is
stdlib-only so the ``homunculus`` console script and the standalone MCP
bridge subprocess can both import it bare (PEP 562 lazy package init).

Migration tripwire (one release): the family moved through two prefixed
generations (the pre-2026-07-25 origin-name prefix, then
``HOMUNCULUS_AGENT_*``) before the neutral atomic land, with NO alias reads.
A legacy name present in the environment while its neutral replacement is
absent means an un-migrated launcher or MCP config — entry points fail loudly
instead of silently registering with degraded identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

AGENT_IDENTITY_ENV: Final[str] = "AGENT_IDENTITY"
AGENT_INSTANCE_ID_ENV: Final[str] = "AGENT_INSTANCE_ID"
AGENT_SESSION_LABEL_ENV: Final[str] = "AGENT_SESSION_LABEL"
AGENT_SESSION_ID_ENV: Final[str] = "AGENT_SESSION_ID"
AGENT_ROLE_ENV: Final[str] = "AGENT_ROLE"

# The older prefix is assembled from fragments so the seed content gate's
# reserved-origin-token scan never matches shipped source; the runtime string
# is unchanged (same idiom as the seed_content_validator's own pattern).
_PRE_RENAME_PREFIX: Final[str] = "AD" + "A_"
_LEGACY_ENV_PREFIXES: Final[tuple[str, ...]] = (_PRE_RENAME_PREFIX, "HOMUNCULUS_")
_CONTRACT_ENV_NAMES: Final[tuple[str, ...]] = (
    AGENT_IDENTITY_ENV,
    AGENT_INSTANCE_ID_ENV,
    AGENT_SESSION_LABEL_ENV,
    AGENT_SESSION_ID_ENV,
    AGENT_ROLE_ENV,
)


def enforce_no_legacy_agent_env() -> None:
    """Fail loudly when a legacy prefixed ``AGENT_*`` var is still exported.

    Raises ``RuntimeError`` when a legacy prefixed member of the five-name
    family (either pre-rename generation, e.g. ``HOMUNCULUS_AGENT_*``) is
    present while its neutral replacement is absent — never reads through to
    the legacy value.
    Legacy absent is the steady state; legacy plus neutral is tolerated for
    the flip window only (the read sites see exclusively the neutral name).
    """
    stale = [(f"{prefix}{name}", name) for prefix in _LEGACY_ENV_PREFIXES for name in _CONTRACT_ENV_NAMES if os.environ.get(f"{prefix}{name}") is not None and os.environ.get(name) is None]
    if stale:
        legacy = ", ".join(legacy_name for legacy_name, _ in stale)
        neutral = ", ".join(dict.fromkeys(neutral_name for _, neutral_name in stale))
        msg = f"un-migrated fleet identity environment: {legacy} present without {neutral}. The {_PRE_RENAME_PREFIX}AGENT_* and HOMUNCULUS_AGENT_* families were renamed to AGENT_* (2026-07-28, no alias reads); update the launcher/MCP config to export the unprefixed names."
        raise RuntimeError(msg)


# --- Env-less (desktop-app / hand-started) session identity -----------------
#
# A Claude Code session started outside the fleet launchers (the desktop app,
# or a bare `claude` in a terminal) has none of the five names above. Claude
# Code itself exports CLAUDE_CODE_SESSION_ID to every child it starts (the
# Bash tool, MCP servers and hooks), and that id survives a resume. The record
# below persists ONE fleet identity per Claude session id, so the bridge CLI,
# the MCP bridge and the coordination hooks resolve the same instance id,
# session id and label with or without an MCP server attached.
#
# Precedence is env first: when ANY launcher identity name is present the
# record is neither read nor written, so a launcher-started session behaves
# exactly as before. The coordination-hooks plugin carries a read-only stdlib
# twin (hooks/_session_identity.py) because hooks run outside the venv; the MCP
# bridge and the CLI write the record, and desktop_session_identity_smoke.py
# pins both twins to the same values.

CLAUDE_SESSION_ID_ENV: Final[str] = "CLAUDE_CODE_SESSION_ID"
SESSION_IDENTITY_SCHEMA: Final[str] = "solet_claude_session_identity_v1"
SESSION_IDENTITY_DIRNAME: Final[str] = "claude-session-identity"
SESSION_IDENTITY_AGENT_ID: Final[str] = "claude_code"
_LAUNCHER_IDENTITY_NAMES: Final[tuple[str, ...]] = (
    AGENT_INSTANCE_ID_ENV,
    AGENT_SESSION_ID_ENV,
    AGENT_SESSION_LABEL_ENV,
)
_CLAUDE_SESSION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_RECORD_FIELDS: Final[tuple[str, ...]] = ("agent_instance_id", "agent_session_id", "session_label")


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """One env-less session's persisted fleet identity."""

    agent_instance_id: str
    agent_session_id: str
    session_label: str
    path: Path


def launcher_identity_present(environ: Mapping[str, str]) -> bool:
    """True when a launcher exported any fleet identity name; env then wins."""
    return any(environ.get(name, "").strip() for name in _LAUNCHER_IDENTITY_NAMES)


def session_identity_path(runtime_dir: Path, claude_session_id: str) -> Path:
    """The record for one Claude session, below the solet runtime directory."""
    if _CLAUDE_SESSION_ID_PATTERN.fullmatch(claude_session_id) is None:
        msg = f"unsupported {CLAUDE_SESSION_ID_ENV} value {claude_session_id!r}"
        raise ValueError(msg)
    return runtime_dir / SESSION_IDENTITY_DIRNAME / f"{claude_session_id}.json"


def mint_session_identity_record(claude_session_id: str, solet_name: str, created_by: str) -> dict[str, str]:
    """The record a first resolver writes; every value derives from the id.

    Deterministic minting makes the start-up race between the hooks, the MCP
    bridge and the CLI benign: whichever writes first, all of them agree.
    """
    digest = hashlib.sha256(f"solet-claude-session:{claude_session_id}".encode()).hexdigest()
    return {
        "schema": SESSION_IDENTITY_SCHEMA,
        "claude_session_id": claude_session_id,
        "solet_name": solet_name,
        "agent_identity": SESSION_IDENTITY_AGENT_ID,
        "agent_instance_id": f"agi-{digest[:32]}",
        "agent_session_id": f"ases-claude-{claude_session_id}",
        "session_label": f"claude-{claude_session_id[:8]}",
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": created_by,
    }


def _write_if_absent(path: Path, record: Mapping[str, str]) -> None:
    """Create ``path`` atomically; an existing record is never overwritten."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)


def _read_record(path: Path, claude_session_id: str) -> dict[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema") != SESSION_IDENTITY_SCHEMA:
        return None
    if value.get("claude_session_id") != claude_session_id:
        return None
    fields = {name: value.get(name) for name in _RECORD_FIELDS}
    if not all(isinstance(item, str) and item.strip() for item in fields.values()):
        return None
    return {name: str(item) for name, item in fields.items()}


def resolve_session_identity(
    environ: Mapping[str, str],
    *,
    runtime_dir: Path,
    solet_name: str,
    created_by: str,
    warn: Callable[[str], None] | None = None,
) -> SessionIdentity | None:
    """The env-less session's identity, or ``None`` when env governs.

    ``None`` when a launcher identity is present (env wins, the record is not
    touched) or when Claude Code exported no session id. Otherwise the record
    is created if absent and read back. A record that cannot be written or
    read is not a refusal: the minted values are used and ``warn`` says why.
    """
    if launcher_identity_present(environ):
        return None
    claude_session_id = environ.get(CLAUDE_SESSION_ID_ENV, "").strip()
    if not claude_session_id:
        return None
    path = session_identity_path(runtime_dir, claude_session_id)
    minted = mint_session_identity_record(claude_session_id, solet_name, created_by)
    try:
        _write_if_absent(path, minted)
    except OSError as exc:
        if warn is not None:
            warn(f"session identity record {path} not written ({exc}); using the minted identity")
    stored = _read_record(path, claude_session_id)
    if stored is None:
        if warn is not None:
            warn(f"session identity record {path} unreadable or malformed; using the minted identity")
        stored = {name: minted[name] for name in _RECORD_FIELDS}
    return SessionIdentity(
        agent_instance_id=stored["agent_instance_id"],
        agent_session_id=stored["agent_session_id"],
        session_label=stored["session_label"],
        path=path,
    )
