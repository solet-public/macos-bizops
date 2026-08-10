#!/usr/bin/env python3
"""T2 (fleet-token-efficiency lane, 2026-08-05) -- PostToolUse heartbeat.

Seat's redesign ruling (2026-08-05, role thread): the FIRST heartbeat
design (a detached background shell loop calling report_alive on an
interval) recreated a measured live defect -- pid-32482 survived a fleet
``/clear`` and kept stamping a ``live`` row for an effectively-dead session
for ~10 hours, because a standalone loop has no coupling to whether the
CLI process it was started for is actually still doing anything. This hook
is the fix: it has NO persistent process of its own. It only runs when the
CLI itself spawns it, synchronously, as part of an actual tool-call
lifecycle -- so it dies with the context it vouches for BY CONSTRUCTION.
An idle, cleared-to-nothing, or dead session simply never fires this hook
again, and nothing is left running to keep stamping on its behalf.

Throttled to at most once per :data:`_THROTTLE_SECONDS` via a per-worker
local marker file's mtime (cheap -- a stat(), no CLI/network round trip on
most firings) rather than checking the platform on every single tool call.
When the throttle allows a stamp, shells out to ``homunculus call
plugin::agent_messaging_plugin::report_alive`` (PATH-resolved, same
convention this repo's other hooks already rely on for ``python3`` --
report_alive takes ``agent_instance_id`` as an explicit argument, so the
bare-CLI no-caller-identity trap does not apply, per the T1 ruling's own
recon finding).

Known accepted gap (seat's own framing, not engineered around): a single
tool call longer than the worker's report_by window still trips the
overdue alarm mid-call, since this hook only fires AFTER a tool call
completes. Rare; disclose, don't chase.

Non-fatal by design, same contract as capture_session_mapping.py: a
missing env var, a report_alive call failure, or an unwritable marker
file warns on stderr and exits 0 -- a broken heartbeat must never cost a
worker its tool call.

Stdlib-only -- fires outside the venv, mirrors this repo's other hooks
(.claude/hooks/capture_session_mapping.py,
.claude/hooks/headless_tool_allowlist_gate.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_MARKER_DIR_ENV = "AGENT_HEARTBEAT_MARKER_DIR"
_INSTANCE_ID_ENV = "AGENT_INSTANCE_ID"

# Comfortably under DEFAULT_REPORT_BY_SECONDS (300s, session_lifecycle_store.py)
# -- a worker whose spawn requested a larger custom report_by window still
# gets stamped well within it. Deliberately NOT configurable per-spawn (T2's
# brief scope is "small"); a future slice can widen this if a lane's window
# is ever set below this value.
_THROTTLE_SECONDS = 180.0

_REPORT_ALIVE_PROCESS_KEY = "plugin::agent_messaging_plugin::report_alive"


def _warn(message: str) -> None:
    try:
        print(f"[heartbeat-report-alive] {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 -- telemetry strictly best-effort
        pass


def _marker_path(marker_dir: str, agent_instance_id: str) -> Path:
    return Path(marker_dir) / f"{agent_instance_id}.stamp"


def _throttled(marker_path: Path) -> bool:
    """True means "skip -- stamped recently enough". A marker that doesn't
    exist, or that fails to stat for any reason, is never throttled (the
    safe default is to attempt a stamp, not to silently skip forever)."""
    try:
        age = time.time() - marker_path.stat().st_mtime
    except OSError:
        return False
    return age < _THROTTLE_SECONDS


def _touch_marker(marker_path: Path) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(time.time()))


def _call_report_alive(agent_instance_id: str) -> bool:
    payload = json.dumps({
        "agent_instance_id": agent_instance_id,
        "status": "working",
        "status_note": "t2-posttooluse-heartbeat",
    })
    try:
        result = subprocess.run(
            ["homunculus", "call", _REPORT_ALIVE_PROCESS_KEY, payload],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"report_alive subprocess failed to run: {exc}")
        return False
    if result.returncode != 0:
        _warn(f"report_alive exited {result.returncode}: {result.stderr.strip()[:200]}")
        return False
    return True


def main() -> int:
    marker_dir = os.environ.get(_MARKER_DIR_ENV, "").strip()
    agent_instance_id = os.environ.get(_INSTANCE_ID_ENV, "").strip()

    if not agent_instance_id:
        # No fleet instance id at all: genuinely not a fleet-managed spawn
        # (or the adapter never wired identity). Nothing to report against.
        _warn(
            f"{_INSTANCE_ID_ENV} not set -- not a fleet-managed spawn, or "
            "the adapter didn't wire it; skipping",
        )
        return 0

    if not marker_dir:
        # MIGRATION-FAIL-OPEN GUARD, not a rename fix (2026-08-08). An
        # instance id alone proves this session IS fleet-managed, so a
        # missing marker dir here is never "unmanaged" -- it is managed
        # AND MIS-WIRED (e.g. spawned before a wiring-variable rename
        # landed; a process's env is frozen at spawn and cannot pick up a
        # renamed variable in place). The prior combined check
        # (`not marker_dir or not agent_instance_id`) could not tell this
        # apart from the genuinely-unmanaged case and silently reclassified
        # the whole running fleet as unmanaged after the 2026-08-08
        # ANANTA_HEARTBEAT_MARKER_DIR -> AGENT_HEARTBEAT_MARKER_DIR rename,
        # with liveness reporting stopped and no error anywhere. Fail LOUD
        # on the contradiction, but still report: report_alive only needs
        # the instance id -- the marker dir exists solely for throttling.
        # There is nowhere to stamp a throttle marker here, so every firing
        # in this degraded state calls report_alive unthrottled. Deliberate,
        # not an oversight: report_alive is a cheap local platform call with
        # its own timeout/non-fatal handling (not an LLM call), this hook
        # only fires once per completed tool call (naturally rate-bounded,
        # not a background loop), and the affected population is fixed and
        # only shrinks -- every session spawned after a correctly-wired
        # marker dir env is unaffected. Restoring liveness immediately here
        # beats waiting for the whole fleet to cycle.
        _warn(
            f"{_INSTANCE_ID_ENV} is set but {_MARKER_DIR_ENV} is NOT -- this "
            "is a MANAGED session with a mis-wired heartbeat marker (its env "
            "was frozen at spawn before a wiring-variable rename landed), "
            "not an unmanaged one. Reporting unthrottled: no marker dir to "
            "throttle against.",
        )
        _call_report_alive(agent_instance_id)
        return 0

    marker_path = _marker_path(marker_dir, agent_instance_id)
    if _throttled(marker_path):
        return 0

    if _call_report_alive(agent_instance_id):
        try:
            _touch_marker(marker_path)
        except OSError as exc:
            _warn(f"failed to write marker file: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
