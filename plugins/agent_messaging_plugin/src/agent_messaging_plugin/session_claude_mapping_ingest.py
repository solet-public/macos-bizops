"""T1 usage-capture lane (2026-08-05, workbench
the 2026-08-05 usage-capture ruling) — drains the SessionStart
hook's file-per-firing spool (``.claude/hooks/capture_session_mapping.py``,
``ANANTA_SESSION_MAPPING_SPOOL_DIR``) into ``session_claude_mapping``
(schema.py + session_claude_mapping_store.py).

Crash-safe by construction (ruling Q1(b)): a spool file is deleted ONLY
after its upsert durably completes; :func:`upsert_session_claude_mapping`
conflicts on ``(agent_instance_id, claude_session_id, captured_at)`` — the
exact triple the spool filename encodes — so re-processing a file that
survived a crash before its post-write delete is a no-op re-upsert, never
a duplicate row. A malformed file (bad JSON, missing required field) is
logged and left in place for investigation — never silently deleted,
never fatal to the rest of the drain. A missing/nonexistent spool dir
(``APP_HOME`` unset on this process, or no firing has happened yet) is a
valid steady state, not an error.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .schema import (
    CAPTURE_SOURCE_HOOK_STARTUP,
    CAPTURE_SOURCE_INIT_EVENT,
)
from .session_claude_mapping_store import (
    list_session_claude_mappings,
    upsert_session_claude_mapping,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_SPOOL_DIR_ENV = "ANANTA_SESSION_MAPPING_SPOOL_DIR"
_REQUIRED_FIELDS = ("agent_instance_id", "claude_session_id", "captured_at", "capture_source")


def _resolve_spool_dir() -> Path | None:
    """The env var wins when declared (tests, standalone contexts); otherwise
    fall back to the SAME ``APP_HOME`` derivation the host adapters use when
    exporting the var to spawned workers (``headless_adapter.
    _resolve_session_mapping_spool_dir``). The fallback is load-bearing on the
    platform's own process: the sweep tick and the on-demand verb run inside
    the solet, whose environment carries ``APP_HOME`` but NOT the worker
    env var — 2026-08-05 live acceptance caught the env-var-only read
    no-opping every drain while spool files accumulated silently. ``None``
    only when neither is set."""
    spool_dir = os.environ.get(_SPOOL_DIR_ENV, "").strip()
    if spool_dir:
        return Path(spool_dir)
    app_home = os.environ.get("APP_HOME", "").strip()
    if not app_home:
        return None
    return Path(app_home) / "data" / "session_claude_mapping_spool"


def _load_record(path: Path) -> dict[str, str] | None:
    """``None`` means malformed — the caller logs and leaves the file in
    place (never silently deleted, per the ruling's non-fatal contract)."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "session_claude_mapping spool file %s unreadable/invalid JSON: %s",
            path,
            exc,
        )
        return None
    if not isinstance(raw, dict) or not all(raw.get(field) for field in _REQUIRED_FIELDS):
        logger.warning(
            "session_claude_mapping spool file %s missing required field(s) %s",
            path,
            _REQUIRED_FIELDS,
        )
        return None
    return {field: str(raw[field]) for field in _REQUIRED_FIELDS}


def _cross_check_init_event(state: StateManagementInterface, agent_instance_id: str) -> None:
    """Ruling addendum (slice D, 2026-08-05) -- pair ``init_event`` rows
    ONLY against ``hook:startup`` rows, per ``agent_instance_id``, both-exist
    condition. ``hook:clear``/``hook:resume`` rows are deliberately OUT OF
    SCOPE: a /clear mints a fresh claude_session_id with no matching init
    event by design, so pairing against them would false-WARN on every
    /clear (the exact false-positive trap the ruling names). A missing side
    -- tmux has no init event; the hook may not have fired yet -- is
    silently NOT a mismatch, never logged. The two observations are
    independent witnesses and are never merged into one row."""
    rows = list_session_claude_mappings(state, agent_instance_id)
    startup_ids = {r["claude_session_id"] for r in rows if r.get("capture_source") == CAPTURE_SOURCE_HOOK_STARTUP}
    init_event_ids = {r["claude_session_id"] for r in rows if r.get("capture_source") == CAPTURE_SOURCE_INIT_EVENT}
    if not startup_ids or not init_event_ids:
        return
    if startup_ids.isdisjoint(init_event_ids):
        logger.warning(
            "session_claude_mapping cross-check MISMATCH for agent_instance_id=%s: hook:startup claude_session_id(s)=%s vs init_event claude_session_id(s)=%s",
            agent_instance_id,
            sorted(startup_ids),
            sorted(init_event_ids),
        )


def drain_session_claude_mapping_spool(state: StateManagementInterface) -> dict[str, object]:
    """List the spool dir, upsert each well-formed firing, delete each file
    ONLY after its upsert durably completes. Testable/on-demand (the
    ``@platform_process`` verb calls this directly) AND wired into the
    platform sweep tick (``plugin.py``'s ``_run_session_lifecycle_sweep``),
    the same call site that fires armed dependency edges.
    """
    spool_dir = _resolve_spool_dir()
    if spool_dir is None or not spool_dir.is_dir():
        return {"files_seen": 0, "upserted": 0, "skipped_malformed": 0}

    files_seen = 0
    upserted = 0
    skipped_malformed = 0
    touched_instance_ids: set[str] = set()
    for path in sorted(spool_dir.glob("*.json")):
        files_seen += 1
        record = _load_record(path)
        if record is None:
            skipped_malformed += 1
            continue
        upsert_session_claude_mapping(
            state,
            agent_instance_id=record["agent_instance_id"],
            claude_session_id=record["claude_session_id"],
            captured_at=record["captured_at"],
            capture_source=record["capture_source"],
        )
        upserted += 1
        touched_instance_ids.add(record["agent_instance_id"])
        try:
            path.unlink()
        except OSError as exc:
            # The upsert already landed durably -- a delete failure just
            # means this file gets safely re-ingested (idempotent upsert)
            # next drain, never a lost or duplicated row.
            logger.warning(
                "session_claude_mapping spool file %s upserted but could not be deleted: %s",
                path,
                exc,
            )

    # Slice D (ruling addendum): the cross-check only needs to re-evaluate
    # instances that got a fresh row THIS call -- an already-settled pair
    # from a prior drain has nothing new to compare.
    for agent_instance_id in touched_instance_ids:
        _cross_check_init_event(state, agent_instance_id)

    return {"files_seen": files_seen, "upserted": upserted, "skipped_malformed": skipped_malformed}


__all__ = [
    "drain_session_claude_mapping_spool",
]
