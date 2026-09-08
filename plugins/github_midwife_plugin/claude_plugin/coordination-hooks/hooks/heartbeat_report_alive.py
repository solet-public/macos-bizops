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
When the throttle allows a stamp, shells out to ``solet-bridge call
plugin::agent_messaging_plugin::report_alive`` -- PATH-resolved, argv
literally ``["solet-bridge", "call", ...]`` per SECURITY.md's disclosed contract,
but the PATH it resolves against is widened (see :func:`_solet_call_env`)
to also search ``AGENT_WAKE_CLI``'s directory when that directory actually
holds a ``solet-bridge`` binary (2026-08-16: a worker whose PATH excludes the venv
bin dir silently FileNotFoundError'd on a plain PATH lookup). report_alive
takes ``agent_instance_id`` as an explicit argument, so the bare-CLI
no-caller-identity trap does not apply, per the T1 ruling's own recon
finding).

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

# INTERPRETER FLOOR. These hooks are Python 3.13 source and use datetime.UTC
# (3.11+). Claude Code launches them with a bare `python3`, which resolves from
# PATH -- on a stock macOS that is frequently the system 3.9, and the resulting
# ImportError traceback surfaced to the operator as a hook error on EVERY tool
# call. Measured 2026-08-20: 8 of 20 shipped hook modules failed to import.
#
# Placed AFTER `from __future__` (which must stay the first statement) and
# BEFORE the first 3.11+ import, because an ImportError at module level cannot
# be caught by anything inside this file. Exits 0 and SILENTLY: the shipped
# contract is that a session which cannot run these hooks gets zero output and
# zero errors, and a diagnostic here would reproduce the very symptom it fixes.
# A floor, not a compatibility shim -- nothing is emulated or back-ported.
import sys

if sys.version_info < (3, 11):  # noqa: UP036 -- see above; ruff assumes
    # the project's py313 target, but this file ships to an ADOPTER's machine
    # and is launched by whatever `python3` their PATH resolves.
    raise SystemExit(0)


import json
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
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
_WAKE_CLI_ENV = "AGENT_WAKE_CLI"


def _warn(message: str) -> None:
    try:
        print(f"[heartbeat-report-alive] {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 -- telemetry strictly best-effort
        pass


def _marker_path(marker_dir: str, agent_instance_id: str) -> Path:
    return Path(marker_dir) / f"{agent_instance_id}.stamp"


def _fallback_marker_dir() -> str | None:
    """Return the established temporary marker root for a mis-wired worker.

    A managed worker with no declared marker dir still needs a stable place
    for its throttle marker and D-5.3 carry-forward record.  The sibling
    rotation hook already uses this temp-root pattern for precisely that
    frozen-environment migration case; it is deliberately not a guessed
    project-relative path.
    """
    path = Path(tempfile.gettempdir()) / "agent_heartbeat_markers"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warn(f"could not create fallback marker root {path}: {exc}")
        return None
    return str(path)


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


def _failure_path(marker_path: Path) -> Path:
    return marker_path.with_suffix(".failures.json")


def _pending_failures(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _pending_failure_count(pending: dict[str, object]) -> int:
    value = pending.get("count", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _record_failure(path: Path, reason: str) -> None:
    previous = _pending_failures(path)
    count = _pending_failure_count(previous) + 1
    now = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "count": count,
        "first_failure_at": str(previous.get("first_failure_at") or now),
        "last_reason": reason,
    }))


def _solet_call_env() -> dict[str, str]:
    """``os.environ``, with PATH APPENDED by ``AGENT_WAKE_CLI``'s directory
    when that directory actually contains a file named ``solet-bridge`` --
    SECURITY.md's disclosed contract for this hook keeps argv literally
    ``["solet-bridge", "call", ...]`` (PATH-resolved, from the session's own
    environment, same category as before); this widens WHICH directories
    PATH searches, not what gets exec'd by name.

    APPEND, not prepend (2026-08-16, cross-session review): a prepend would
    make the release venv's bin dir win PATH resolution for EVERY lookup in
    this subprocess and anything it spawns, not just ``solet-bridge`` -- that
    directory also carries ``python3``/``pip``, so a prepend would silently
    change which of those a child process resolves too, a behavior change
    beyond "find the right solet-bridge" with no signal in the diff's intent.
    Append fixes the identical missing-solet-bridge case (a PATH that lacks solet-bridge
    entirely resolves it either way, first match or last) while never
    shadowing an existing resolution -- it only ever adds a location PATH
    lookup falls through to, never reorders one already there.

    2026-08-16 dark-gauge root cause: a bare ``"solet-bridge"`` lookup against the
    UNMODIFIED PATH silently ``FileNotFoundError``s on a worker whose PATH
    excludes the venv bin dir -- caught by the ``except OSError`` below,
    warned to stderr (nothing reads it), exit 0. The throttle marker still
    gets touched upstream of this call, so the failure looks identical to a
    healthy tick from the outside: "stamp updates, no report ever lands."
    Measured live, reproduced by hand.

    ``AGENT_WAKE_CLI`` is exported at spawn time pointing into a versioned
    release directory, and a deploy reaps old releases -- so a long-lived
    worker's export can go DANGLING out from under it (measured live,
    2026-08-16: a worker spawned before a same-day deploy held an
    AGENT_WAKE_CLI naming a release directory that no longer existed). The
    ``is_file()`` guard -- a stat for the FILE, not merely the directory's
    existence -- means a dangling export contributes NOTHING to PATH -- no
    bogus directory gets appended at all -- so a session whose PATH already
    resolves solet-bridge fine is completely unaffected either way; only a session
    that would otherwise fail gains a chance to resolve."""
    cli = os.environ.get(_WAKE_CLI_ENV, "").strip()
    if not cli:
        return dict(os.environ)
    solet_dir = str(Path(cli).parent)
    if not (Path(solet_dir) / "solet-bridge").is_file():
        return dict(os.environ)
    env = dict(os.environ)
    env["PATH"] = f"{env.get('PATH', '')}:{solet_dir}"
    return env


def _report_alive_payload(agent_instance_id: str, pending: dict[str, object]) -> str:
    payload_data: dict[str, object] = {
        "agent_instance_id": agent_instance_id,
        "status": "heartbeat",
        "status_note": "t2-posttooluse-heartbeat",
        "heartbeat_failures_since_last": _pending_failure_count(pending),
    }
    failure_first_at = pending.get("first_failure_at")
    if isinstance(failure_first_at, str) and failure_first_at:
        payload_data["heartbeat_failure_first_at"] = failure_first_at
    failure_last_reason = pending.get("last_reason")
    if isinstance(failure_last_reason, str) and failure_last_reason:
        payload_data["heartbeat_failure_last_reason"] = failure_last_reason
    return json.dumps(payload_data)


def _report_alive_rejection(stdout: str) -> str | None:
    try:
        response = json.loads(stdout)
        action_result = response["result"]
        success = action_result["success"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        return f"unreadable report_alive response: {exc}"
    if success is True:
        return None
    error = action_result.get("error") if isinstance(action_result, dict) else None
    return f"report_alive rejected the ledger write: {error!r}"


def _call_report_alive(agent_instance_id: str, failure_path: Path | None = None) -> bool:
    pending = _pending_failures(failure_path) if failure_path is not None else {}
    payload = _report_alive_payload(agent_instance_id, pending)
    try:
        result = subprocess.run(
            ["solet-bridge", "call", _REPORT_ALIVE_PROCESS_KEY, payload],
            capture_output=True, text=True, timeout=20, check=False,
            env=_solet_call_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"report_alive subprocess failed to run: {exc}")
        if failure_path is not None:
            _record_failure(failure_path, repr(exc))
        return False
    if result.returncode != 0:
        _warn(f"report_alive exited {result.returncode}: {result.stderr.strip()[:200]}")
        if failure_path is not None:
            _record_failure(failure_path, result.stderr.strip()[:200] or f"exit {result.returncode}")
        return False
    detail = _report_alive_rejection(result.stdout)
    if detail is not None:
        _warn(detail)
        if failure_path is not None:
            _record_failure(failure_path, detail)
        return False
    return True


def main() -> int:
    marker_dir = os.environ.get(_MARKER_DIR_ENV, "").strip()
    agent_instance_id = os.environ.get(_INSTANCE_ID_ENV, "").strip()

    if not agent_instance_id:
        # No fleet instance id: not a fleet-managed spawn. SILENT, deliberately.
        #
        # This is the NORMAL case, not an anomaly — the operator launcher
        # (claude_launcher.template) exports the solet name, the session
        # label/id pair and the wake CLI, but NOT this one, which only the
        # fleet spawn adapters wire. So every ordinary operator session took
        # this branch, and this used to _warn() 121 bytes to stderr on EVERY
        # tool call — surfaced to adopters as a hook error per action, on a
        # correctly-installed machine doing nothing wrong (reported
        # 2026-08-20).
        #
        # It also broke the shipped contract that a session which is not part
        # of fleet coordination "must get zero output and zero errors". A
        # condition that holds for the majority of sessions is a state, not a
        # warning; warning on it trains operators to ignore hook output, which
        # is exactly when a real one gets missed.
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
        # The sibling rotation hook already carries this exact migration
        # condition through a temporary marker root.  Reuse that established
        # marker mechanism here: it gives D-5.3's failure record a durable
        # location beside the throttle marker, and lets the next successful
        # report consume it instead of losing the failure at this branch.
        fallback = _fallback_marker_dir()
        if fallback is None:
            _call_report_alive(agent_instance_id)
            return 0
        _warn(
            f"{_INSTANCE_ID_ENV} is set but {_MARKER_DIR_ENV} is NOT -- this "
            "is a MANAGED session with a mis-wired heartbeat marker (its env "
            "was frozen at spawn before a wiring-variable rename landed), "
            f"not an unmanaged one. Using fallback marker root {fallback}.",
        )
        marker_dir = fallback

    marker_path = _marker_path(marker_dir, agent_instance_id)
    if _throttled(marker_path):
        return 0

    failure_path = _failure_path(marker_path)
    if _call_report_alive(agent_instance_id, failure_path):
        try:
            _touch_marker(marker_path)
            failure_path.unlink(missing_ok=True)
        except OSError as exc:
            _warn(f"failed to write marker file: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
