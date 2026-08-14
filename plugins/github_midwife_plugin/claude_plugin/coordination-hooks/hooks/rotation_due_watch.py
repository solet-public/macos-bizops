#!/usr/bin/env python3
"""rotation-systematization P2 slice B (ruling 2, P1 ratification 2026-08-07
-- workbench/2026-08-07_rotation_systematization_findings_rotation-impl.md)
-- PostToolUse hook, sibling to ``heartbeat_report_alive.py``: the
host-independent rotation-due trigger for BOTH the seat and every worker.

Reads this session's OWN ``transcript_path`` (the same stdin field Claude
Code hooks already carry), tails the most recent ``type=assistant`` line's
``message.usage`` block (a zero-lag, no-ingestion-latency proxy for current
context occupancy -- measured live against this session's own transcript
during P1(b)), compares against the declared ceiling in
``agent_messaging_plugin.rotation_thresholds`` (a SINGLE source of truth --
imported directly rather than duplicated, since that module has zero
third-party dependencies and is safe to import without the venv), and on a
threshold crossing NOTIFIES the steward. It never acts (never calls
``clear_session`` or the seat's own rotation helper) and never touches
``report_by``/``report_alive``/any lifecycle-transition verb -- rotation
timing stays a steward/seat decision (ruling 2; brief's own out-of-scope
line: "changing WHEN the fleet rotates as policy... stays with the
seat/operator").

Two independent marker files, distinct purposes (both under the SAME
declared ``AGENT_HEARTBEAT_MARKER_DIR`` this checkout already wires --
composing the existing declared directory rather than requesting new
adapter-side env wiring, per the brief's "prefer composing landed
machinery over new surface" framing):
- THROTTLE (per ``agent_instance_id``): how often this hook even computes
  -- cost control, same shape as ``heartbeat_report_alive.py``'s own
  throttle.
- LATCH (per ``agent_instance_id`` + the CURRENT ``claude_session_id``):
  fires the notification at most once per session-generation. Keying on
  the CURRENT claude_session_id (not just agent_instance_id) means a
  ``/clear`` naturally gets a fresh, absent latch file with no explicit
  reset logic -- the session_claude_mapping capture already re-fires on
  every new session_id (``hook:clear`` etc.), so this hook's own re-fire
  on the same PostToolUse wiring needs no bespoke reset path either.

Steward resolution: ``session_status`` for this ``agent_instance_id`` ->
``spawned_by_role`` when the row exists and carries one (every managed
worker). For a row that doesn't exist (``host=operator`` -- e.g. the seat
itself, never spawned via ``spawn_session``) or carries no
``spawned_by_role``, this falls back to a LOCALLY-SURFACED marker file
(self-notification artifact) rather than failing -- consumption of that
marker on the seat's own next turn is a named follow-on, not built here
(this hook's job is the trigger + delivery attempt, not the read-back
UX).

Notification identity discipline: the message CONTENT carries this
session's ``agent_instance_id``/``session_label`` verbatim as text (never
relies on the transport's own sender-identity field to carry it) --
per this fleet's own measured trap that a bare CLI send drops caller
identity (names route, content binds).

Non-fatal by design, same contract as this checkout's other hooks: any
failure (missing env var, unreadable transcript, unparseable JSON,
``solet`` subprocess failure) warns on stderr and exits 0 -- this
hook must never cost a session its tool call.

Stdlib-only for I/O and subprocess dispatch, EXCEPT the one direct import
of ``agent_messaging_plugin.rotation_thresholds`` (zero-dependency pure
module, safe outside the venv) -- mirrors this repo's other hooks
(``capture_session_mapping.py``, ``heartbeat_report_alive.py``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MARKER_DIR_ENV = "AGENT_HEARTBEAT_MARKER_DIR"
_INSTANCE_ID_ENV = "AGENT_INSTANCE_ID"
_SESSION_LABEL_ENV = "AGENT_SESSION_LABEL"
_PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"

# Comfortably more frequent than the heartbeat's 180s -- rotation-due is a
# slower-moving signal than liveness (context grows over many turns), but
# still needs to catch a boundary reasonably soon after it's crossed. Not
# measured against a real growth-rate curve this pass; a declared default,
# not a guess dressed as one -- named as such.
_THROTTLE_SECONDS = 120.0

_SESSION_STATUS_PROCESS_KEY = "plugin::agent_messaging_plugin::session_status"
_PEER_SEND_PROCESS_KEY = "plugin::agent_messaging_plugin::peer_send_by_name"
# maintenance-verbs M1 (workbench
# 2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3, shape (a)):
# this hook already computes current_tokens/model/ceiling every un-throttled
# tick for the notify path below -- piggybacking a plain state-cache write
# onto the SAME tick (same throttle window governs both) is the whole of
# shape (a)'s "hook-fed cache" design, no new wiring surface needed for
# worker coverage (workers already carry this hook in their spawn-time
# adapter blob, the same precedent heartbeat_report_alive.py set).
_REPORT_CONTEXT_STATUS_PROCESS_KEY = "plugin::agent_messaging_plugin::report_context_status"


def _warn(message: str) -> None:
    try:
        print(f"[rotation-due-watch] {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 -- telemetry strictly best-effort
        pass


def _throttle_marker_path(marker_dir: str, agent_instance_id: str) -> Path:
    return Path(marker_dir) / f"{agent_instance_id}.rotation_due_check.stamp"


def _latch_marker_path(marker_dir: str, agent_instance_id: str, claude_session_id: str) -> Path:
    return Path(marker_dir) / f"{agent_instance_id}__{claude_session_id}.rotation_due_latch"


def _fallback_marker_path(marker_dir: str, agent_instance_id: str, claude_session_id: str) -> Path:
    return Path(marker_dir) / f"{agent_instance_id}__{claude_session_id}.rotation_due_selfnotify.json"


def is_throttled(marker_path: Path, *, now: float, throttle_seconds: float = _THROTTLE_SECONDS) -> bool:
    """True means "skip -- computed recently enough". A marker that
    doesn't exist, or that fails to stat for any reason, is never
    throttled (the safe default is to attempt a compute, matching
    ``heartbeat_report_alive.py``'s own ``_throttled`` contract)."""
    try:
        age = now - marker_path.stat().st_mtime
    except OSError:
        return False
    return age < throttle_seconds


def touch_marker(marker_path: Path) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(time.time()))


def find_last_assistant_usage(transcript_path: str) -> tuple[str, dict[str, Any]] | None:
    """The most recent ``type=assistant`` line's ``(model, usage)`` pair
    from the transcript JSONL, scanning from the end. ``None`` when the
    file is unreadable, empty, or carries no usage-bearing assistant line
    yet (a brand-new session before its first turn completes) -- never
    raises, matching this hook's non-fatal contract."""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in reversed(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        return str(message.get("model") or ""), usage
    return None


def sum_context_tokens(usage: dict[str, Any]) -> int:
    """``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``
    -- the full set of tokens the CLI reports as consumed to produce the
    most recent turn, the same fields ``budget_report.py`` sums server-side
    (measured live against this session's own transcript, P1(b)). Missing
    or non-numeric fields count as 0 -- never raises on a partial usage
    block."""
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
    return total


def build_notification_content(
    *, agent_instance_id: str, session_label: str, model: str,
    current_tokens: int, ceiling: int, threshold_fraction: float,
) -> str:
    """Identity-in-content, per this fleet's own measured trap (names
    route, content binds) -- the subject session's identity is embedded as
    text, never left to the transport's sender-identity field alone."""
    return (
        f"IMPORTANT: rotation-due threshold crossed for agent_instance_id="
        f"{agent_instance_id!r} session_label={session_label!r}. "
        f"model={model!r} current_tokens={current_tokens} ceiling={ceiling} "
        f"threshold_fraction={threshold_fraction} "
        f"(crossed at {current_tokens / ceiling:.1%} of ceiling). "
        "This is a MEASURED SIGNAL, not an action -- rotation timing stays "
        "a steward/seat decision; nothing was cleared or rotated."
    )


def _resolve_plugin_src_path() -> Path | None:
    """``CLAUDE_PROJECT_DIR``-relative only -- unlike the checkout-local
    original this vendored copy has no fixed parent-directory depth to fall
    back on (this file's own depth under ``$CLAUDE_PLUGIN_ROOT/hooks/``
    differs from the checkout original's ``.claude/hooks/`` depth, so a
    ``parents[N]`` guess would silently resolve to the wrong directory on
    an adopter machine rather than fail). Claude Code always sets this env
    var for a real hook invocation; an unset value means skip, never guess."""
    project_dir = os.environ.get(_PROJECT_DIR_ENV, "").strip()
    if not project_dir:
        return None
    return Path(project_dir) / "plugins" / "agent_messaging_plugin" / "src"


def _import_rotation_thresholds() -> Any | None:
    src_path = _resolve_plugin_src_path()
    if src_path is None:
        _warn(f"{_PROJECT_DIR_ENV} not set -- cannot locate rotation_thresholds, skipping")
        return None
    src_path_str = str(src_path)
    if src_path_str not in sys.path:
        sys.path.insert(0, src_path_str)
    try:
        from agent_messaging_plugin import rotation_thresholds  # noqa: PLC0415
    except ImportError as exc:
        _warn(f"could not import rotation_thresholds: {exc}")
        return None
    return rotation_thresholds


def _solet_call(process_key: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["solet", "call", process_key, json.dumps(arguments)],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"solet call {process_key} failed to run: {exc}")
        return None
    if result.returncode != 0:
        _warn(f"solet call {process_key} exited {result.returncode}: {result.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        _warn(f"solet call {process_key} returned unparseable output: {exc}")
        return None


def _resolve_steward_role(agent_instance_id: str) -> str | None:
    """The managed_session row's ``spawned_by_role``, or ``None`` when the
    row doesn't exist (host=operator, e.g. the seat) or carries no
    steward -- callers fall back to local self-notification, they don't
    treat this as an error."""
    envelope = _solet_call(
        _SESSION_STATUS_PROCESS_KEY, {"agent_instance_id": agent_instance_id},
    )
    if envelope is None or envelope.get("status") != "completed":
        return None
    data = ((envelope.get("result") or {}).get("data")) or {}
    role = str(data.get("spawned_by_role") or "").strip()
    return role or None


def _deliver_notification(*, agent_instance_id: str, claude_session_id: str, content: str, marker_dir: str) -> bool:
    """Peer-send to the resolved steward when one exists; otherwise write
    a locally-surfaced marker file. Returns True on any successful
    delivery path (peer-send OR marker write) -- the caller only touches
    the latch on a True return, so a fully-failed delivery attempt can
    retry on the next un-throttled tick instead of being silently
    latched-but-never-delivered."""
    steward_role = _resolve_steward_role(agent_instance_id)
    if steward_role is not None:
        envelope = _solet_call(_PEER_SEND_PROCESS_KEY, {"name": steward_role, "content": content})
        if envelope is not None and envelope.get("status") == "completed":
            return True
        _warn(f"peer_send_by_name to steward role {steward_role!r} failed; falling back to local marker")
    marker_path = _fallback_marker_path(marker_dir, agent_instance_id, claude_session_id)
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({"content": content, "written_at": time.time()}))
    except OSError as exc:
        _warn(f"failed to write local self-notification marker: {exc}")
        return False
    return True


def _read_stdin_payload() -> dict[str, Any] | None:
    """``None`` means "skip, already warned" -- a parse failure is never
    fatal, same contract as ``capture_session_mapping.py``'s own helper of
    the same shape."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001 -- never disrupt a session
        _warn(f"failed to read/parse stdin payload: {exc}")
        return None
    if not isinstance(payload, dict):
        _warn("stdin payload was not a JSON object")
        return None
    return payload


def _fallback_marker_dir() -> str | None:
    """A writable stand-in marker root for the managed-but-mis-wired case.

    Deliberately the OS temp dir rather than a project-relative path: this
    hook cannot rely on ``CLAUDE_PROJECT_DIR`` (measured absent from live
    spawned workers' env), and guessing a profile-relative location would
    invent a convention rather than use one. Returns ``None`` if the
    directory cannot be created, which sends the caller back to skipping --
    the one case where this hook still declines to fire.
    """
    path = Path(tempfile.gettempdir()) / "agent_rotation_due_markers"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warn(f"could not create fallback marker root {path}: {exc} -- skipping")
        return None
    return str(path)


def _resolve_firing_context() -> tuple[str, str, str, str] | None:
    """``(marker_dir, agent_instance_id, transcript_path, claude_session_id)``,
    or ``None`` when this firing should be skipped (missing env, unreadable
    stdin, or a payload missing the fields this hook needs) -- split out of
    :func:`main` to keep it a straight-line dispatcher (radon cc)."""
    marker_dir = os.environ.get(_MARKER_DIR_ENV, "").strip()
    agent_instance_id = os.environ.get(_INSTANCE_ID_ENV, "").strip()
    if not agent_instance_id:
        # No fleet instance id at all: genuinely not a fleet-managed spawn
        # (or the adapter never wired identity). Nobody to rotate, no
        # steward to notify.
        _warn(
            f"{_INSTANCE_ID_ENV} not set -- not a fleet-managed spawn, or "
            "the adapter didn't wire it; skipping",
        )
        return None
    if not marker_dir:
        # MIGRATION-FAIL-OPEN GUARD (2026-08-08), the rotation-side twin of
        # the one in heartbeat_report_alive.py. An instance id alone proves
        # this session IS fleet-managed, so a missing marker dir is never
        # "unmanaged" -- it is managed AND MIS-WIRED: a process's env is
        # frozen at spawn, so a session started before the
        # ANANTA_HEARTBEAT_MARKER_DIR -> AGENT_HEARTBEAT_MARKER_DIR rename
        # landed can never pick the new name up in place.
        #
        # The prior combined check (`not marker_dir or not
        # agent_instance_id`) could not tell the two apart and silently
        # skipped every firing for the whole running fleet -- warning only
        # to stderr, which nothing reads. Measured consequence: the
        # rotation-due signal died fleet-wide the moment that rename
        # landed, and four sessions ran to the edge of auto-compact with no
        # notice ever reaching their steward. The heartbeat survived the
        # identical miswiring ONLY because it had already been given its
        # own fail-open guard, which masked this one: liveness kept
        # reporting, so the fleet looked managed.
        #
        # Unlike the heartbeat, this hook cannot simply proceed without a
        # marker dir. The dir carries the LATCH as well as the throttle,
        # and an unlatched firing would peer_send the steward on EVERY
        # completed tool call above threshold. So fall back to a temp-dir
        # marker root instead: throttle and latch both keep working, the
        # once-per-session notification contract is preserved, and no
        # deprecated variable name is read and no project path convention
        # is invented. Losing this dir (reboot, tmp reaping) costs at most
        # one extra notification per affected session.
        fallback = _fallback_marker_dir()
        if fallback is None:
            return None
        _warn(
            f"{_INSTANCE_ID_ENV} is set but {_MARKER_DIR_ENV} is NOT -- this "
            "is a MANAGED session whose env was frozen at spawn before a "
            f"wiring-variable rename landed, not an unmanaged one. Using "
            f"fallback marker root {fallback}: throttle and latch preserved.",
        )
        marker_dir = fallback
    payload = _read_stdin_payload()
    if payload is None:
        return None
    transcript_path = str(payload.get("transcript_path") or "")
    claude_session_id = str(payload.get("session_id") or "")
    if not transcript_path or not claude_session_id:
        _warn("stdin payload carried no transcript_path/session_id -- skipping")
        return None
    return marker_dir, agent_instance_id, transcript_path, claude_session_id


def _report_context_status(
    *, agent_instance_id: str, claude_session_id: str, model: str,
    current_tokens: int, ceiling: int,
) -> None:
    """Best-effort cache write for ``session_context_status`` (shape (a)) --
    non-fatal by this hook's own standing contract: a failed report here
    must never cost the notify path below, so failures warn to stderr and
    the caller does not branch on the return."""
    envelope = _solet_call(
        _REPORT_CONTEXT_STATUS_PROCESS_KEY,
        {
            "agent_instance_id": agent_instance_id,
            "claude_session_id": claude_session_id,
            "model": model,
            "current_tokens": current_tokens,
            "ceiling": ceiling,
            "measured_at": datetime.now(UTC).isoformat(),
        },
    )
    if envelope is None or envelope.get("status") != "completed":
        _warn(f"report_context_status did not complete cleanly: {json.dumps(envelope)[:300]}")


def _resolve_usage(transcript_path: str) -> tuple[str, int, Any] | None:
    """``(model, current_tokens, rotation_thresholds module)``, or ``None``
    when there is no usage-bearing assistant line yet or the module import
    fails -- shared by the (unconditional, every-tick) cache report and the
    (latch-gated, once-per-generation) notify path so each tick reads the
    transcript file exactly once, not twice."""
    found = find_last_assistant_usage(transcript_path)
    if found is None:
        return None
    model, usage = found
    rotation_thresholds = _import_rotation_thresholds()
    if rotation_thresholds is None:
        return None
    return model, sum_context_tokens(usage), rotation_thresholds


def _check_and_notify(
    *, marker_dir: str, agent_instance_id: str, claude_session_id: str,
    latch_path: Path, model: str, current_tokens: int, rotation_thresholds: Any,
) -> None:
    """The threshold-and-notify half of a firing (post throttle/latch
    gating) -- split out of :func:`main` to keep it a straight-line
    dispatcher (radon cc). Takes the ALREADY-resolved usage tuple (see
    :func:`_resolve_usage`) rather than a transcript path -- this is the
    LATCH-GATED half (``main`` never calls it once the latch exists for this
    session generation), so it must not be where the cache report lives;
    that runs unconditionally in :func:`main` before the latch check."""
    if not rotation_thresholds.is_rotation_due(model=model, current_tokens=current_tokens):
        return

    content = build_notification_content(
        agent_instance_id=agent_instance_id,
        session_label=os.environ.get(_SESSION_LABEL_ENV, "").strip(),
        model=model,
        current_tokens=current_tokens,
        ceiling=rotation_thresholds.resolve_ceiling(model),
        threshold_fraction=rotation_thresholds.ROTATION_THRESHOLD_FRACTION,
    )
    delivered = _deliver_notification(
        agent_instance_id=agent_instance_id, claude_session_id=claude_session_id,
        content=content, marker_dir=marker_dir,
    )
    if delivered:
        touch_marker(latch_path)


def main() -> int:
    context = _resolve_firing_context()
    if context is None:
        return 0
    marker_dir, agent_instance_id, transcript_path, claude_session_id = context

    throttle_path = _throttle_marker_path(marker_dir, agent_instance_id)
    if is_throttled(throttle_path, now=time.time()):
        return 0
    touch_marker(throttle_path)

    resolved = _resolve_usage(transcript_path)
    if resolved is None:
        return 0
    model, current_tokens, rotation_thresholds = resolved

    # Cache report rides EVERY un-throttled tick, UNCONDITIONALLY -- deliberately
    # ahead of the latch check below, which only gates the once-per-generation
    # notify. session_context_status must answer for a session nowhere near
    # rotation-due too, and must keep refreshing after the one-time notify has
    # already latched for this session generation.
    _report_context_status(
        agent_instance_id=agent_instance_id, claude_session_id=claude_session_id,
        model=model, current_tokens=current_tokens,
        ceiling=rotation_thresholds.resolve_ceiling(model),
    )

    latch_path = _latch_marker_path(marker_dir, agent_instance_id, claude_session_id)
    if latch_path.exists():
        return 0

    _check_and_notify(
        marker_dir=marker_dir, agent_instance_id=agent_instance_id,
        claude_session_id=claude_session_id, latch_path=latch_path,
        model=model, current_tokens=current_tokens, rotation_thresholds=rotation_thresholds,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
