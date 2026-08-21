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
When the throttle allows a stamp, shells out to ``solet call
plugin::agent_messaging_plugin::report_alive`` -- PATH-resolved, argv
literally ``["solet", "call", ...]`` per SECURITY.md's disclosed contract,
but the PATH it resolves against is widened (see :func:`_solet_call_env`)
to also search ``AGENT_WAKE_CLI``'s directory when that directory actually
holds a ``solet`` binary (2026-08-16: a worker whose PATH excludes the venv
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
_WAKE_CLI_ENV = "AGENT_WAKE_CLI"


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


def _solet_call_env() -> dict[str, str]:
    """``os.environ``, with PATH APPENDED by ``AGENT_WAKE_CLI``'s directory
    when that directory actually contains a file named ``solet`` --
    SECURITY.md's disclosed contract for this hook keeps argv literally
    ``["solet", "call", ...]`` (PATH-resolved, from the session's own
    environment, same category as before); this widens WHICH directories
    PATH searches, not what gets exec'd by name.

    APPEND, not prepend (2026-08-16, cross-session review): a prepend would
    make the release venv's bin dir win PATH resolution for EVERY lookup in
    this subprocess and anything it spawns, not just ``solet`` -- that
    directory also carries ``python3``/``pip``, so a prepend would silently
    change which of those a child process resolves too, a behavior change
    beyond "find the right solet" with no signal in the diff's intent.
    Append fixes the identical missing-solet case (a PATH that lacks solet
    entirely resolves it either way, first match or last) while never
    shadowing an existing resolution -- it only ever adds a location PATH
    lookup falls through to, never reorders one already there.

    2026-08-16 dark-gauge root cause: a bare ``"solet"`` lookup against the
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
    resolves solet fine is completely unaffected either way; only a session
    that would otherwise fail gains a chance to resolve."""
    cli = os.environ.get(_WAKE_CLI_ENV, "").strip()
    if not cli:
        return dict(os.environ)
    solet_dir = str(Path(cli).parent)
    if not (Path(solet_dir) / "solet").is_file():
        return dict(os.environ)
    env = dict(os.environ)
    env["PATH"] = f"{env.get('PATH', '')}:{solet_dir}"
    return env


def _call_report_alive(agent_instance_id: str) -> bool:
    payload = json.dumps({
        "agent_instance_id": agent_instance_id,
        "status": "working",
        "status_note": "t2-posttooluse-heartbeat",
    })
    try:
        result = subprocess.run(
            ["solet", "call", _REPORT_ALIVE_PROCESS_KEY, payload],
            capture_output=True, text=True, timeout=20, check=False,
            env=_solet_call_env(),
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
