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

THREE COPIES OF THIS FILE EXIST, AND THE ONE YOU ARE READING IS PROBABLY
NOT THE ONE THAT RAN. Editing any one of them changes nothing about the
other two:

1. ``.claude/hooks/rotation_due_watch.py`` -- the CHECKOUT copy, wired TWO
   independent ways: this repo's own ``.claude/settings.json`` (PostToolUse,
   no matcher), AND, for a SPAWNED WORKER, a generated ``--settings`` blob
   carrying this file's ABSOLUTE, spawn-time-resolved path (see
   ``agent_messaging_plugin.headless_adapter._resolve_worker_hook_path``,
   used by the tmux driver too: rung 1 is ``<repo_root>/.claude/hooks/``
   and always wins when present, rung 2 is the vendored copy below, and
   the version-keyed cache is DELIBERATELY never used because of exactly
   the staleness trap described further down). So a spawned worker runs
   THIS copy, resolved by absolute path -- it needs no environment
   variable and its ``parents[N]`` arithmetic is correct from that path.
2. ``plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/``
   ``rotation_due_watch.py`` -- the VENDORED SOURCE that is packaged into
   the ``coordination-hooks`` plugin. Nothing executes this path directly;
   it is what an install COPIES FROM.
3. ``~/.claude/plugins/cache/<marketplace>/coordination-hooks/<version>/``
   ``hooks/rotation_due_watch.py`` -- the INSTALLED copy, wired by the
   plugin's own ``hooks/hooks.json`` (PostToolUse, no matcher) whenever
   ``coordination-hooks@<marketplace>`` is enabled at user scope. This is
   what the PLUGIN's registration resolves to -- note it is an ADDITIONAL
   registration, not an alternative one: a session can run copy 1 and copy
   3 in the same tick, and a spawned worker in this checkout does exactly
   that. Do not read "the plugin copy fires" as "the plugin copy is the one
   that fires".

Copies 1 and 2 are held in deliberate content lockstep and differ ONLY
inside ``_resolve_plugin_src_path``/``_import_rotation_thresholds`` -- see
the docstring on the vendored copy's ``_resolve_plugin_src_path`` for why
(it has no fixed parent-directory depth to fall back on, so it must resolve
from ``CLAUDE_PROJECT_DIR`` alone or skip, rather than guess). That
divergence is an ADAPTATION, NOT ROT: a naive dedup into one file breaks
adopter path resolution. Any change to either copy must be mirrored into
the other in the SAME landing, and the diff between them must stay confined
to that one region.

Copy 3 DOES NOT FOLLOW FROM A COMMIT. Install copies the plugin into a
VERSIONED CACHE DIRECTORY; merging to master -- even bumping the manifest
version -- changes nothing about what executes. Only an explicit reinstall
moves the pinned entry in ``~/.claude/plugins/installed_plugins.json`` to a
new ``installPath``. A bump WITHOUT a reinstall is the expected failure
here, and it is silent: the repo reads correct at every version while the
process keeps running old code. Verify a hook change actually reached copy
3 by comparing that file's ``version`` and ``gitCommitSha`` against the
plugin manifest and against master -- NEVER by re-reading the repo file you
just edited.

Copies 1 and 3 both bind PostToolUse with no matcher, and settings sources
MERGE rather than override, so in a project that wires copy 1 while the
plugin is enabled BOTH are registered and both run. They also share their
throttle and latch marker paths (see ``_throttle_marker_path`` and
``_latch_marker_path``), which carry no discriminator for WHICH copy wrote
them, and ``main`` claims the throttle BEFORE performing any work. So
within one ``_THROTTLE_SECONDS`` window exactly one copy serves the tick,
chosen by hook execution order rather than by which copy is current -- a
stale copy 3 can quietly serve the tick and emit a report missing whatever
a newer copy would have sent, while a marker file and a report still appear
exactly as they would on success. If you are debugging a field that "should
be there and isn't", rule this out before suspecting the reporting verb.
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
_PEER_LIST_PROCESS_KEY = "plugin::agent_messaging_plugin::peer_list"
# This copy's own CONTENT GENERATION, reported alongside every snapshot so a
# reader can tell a current copy from an older one that is still being served
# (copies of this file go stale independently -- see the module docstring).
#
# BUMP THIS whenever the reporting content changes, in BOTH repo copies, in
# the same landing. It is not a version of the file, and deliberately not a
# git sha: this file cannot know the commit it was copied from. It answers
# exactly one question -- "is the code that wrote this row as new as the code
# I am reading?" -- and it can only answer it if the bump is not forgotten.
#
# 1 = the generation that first carried reporter attribution (2026-08-16),
#     which is also the first generation carrying the cache-state fields.
# 2 = surface classification resolved (2026-08-17): 'vendored' and 'release'
#     split out of the collapsed 'unknown', and a checkout hook in a
#     SUBDIRECTORY no longer misreports as unrecognised. A row reading
#     generation 1 with surface 'unknown' is therefore AMBIGUOUS by
#     construction -- it may be any of the three surfaces this generation
#     learned to tell apart -- and a row reading generation 2 is not.
_REPORTER_GENERATION = 2
# Staleness bound for the seat's registry row. Basis (measured 2026-08-16 over
# 6 live rows): heartbeat ages were 3s / 11s median / 163s stalest, so this is
# ~1.8x the stalest observed and comfortably above this hook's own 60s poll.
# Far below any realistic pid release-reuse-reregister window. The sample is
# THIN; if a legitimate seat is ever skipped as stale, re-measure the cadence
# for genuinely IDLE sessions before widening it.
_SEAT_REGISTRY_MAX_AGE_SECONDS = 300
_ANCESTOR_WALK_MAX_DEPTH = 12
_CLAUDE_PROCESS_NAME = "claude"


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


def _cache_read_tokens(usage: dict[str, Any]) -> int:
    """``cache_read_input_tokens``, or 0 when absent/non-numeric.

    0 here means "read nothing from cache", which is the cold signal -- so an
    absent field reads as cold rather than as unknown. That is safe ONLY
    because the classifier looks at a sequence and the caller omits the whole
    field set when it cannot parse the transcript; a single odd block cannot
    fabricate a cold verdict on its own.
    """
    raw = usage.get("cache_read_input_tokens")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0
    return int(raw)


def _assistant_call_from_line(raw_line: str) -> dict[str, Any] | None:
    """One transcript line -> ``{"at", "cache_read"}``, or ``None`` to skip."""
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    stamp = record.get("timestamp")
    if not isinstance(message, dict) or not isinstance(stamp, str):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    return {"at": stamp, "cache_read": _cache_read_tokens(usage)}


def read_assistant_calls(transcript_path: str, limit: int = 40) -> list[dict[str, Any]]:
    """The most recent assistant calls as ``{"at": str, "cache_read": int}``,
    oldest-first, for cache-state classification.

    Separate from :func:`find_last_assistant_usage`, which answers a different
    question (the LATEST model + usage). Cache state is a property of a
    SEQUENCE -- one cold call after a long gap is ordinary expiry, repeated
    cold calls across short gaps are not -- so it cannot be read off a single
    block. Bounded to `limit` because the classifier only needs the recent
    tail and a transcript can carry thousands of blocks.

    Never raises, matching this hook's non-fatal contract.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    calls: list[dict[str, Any]] = []
    for raw_line in reversed(lines):
        call = _assistant_call_from_line(raw_line)
        if call is None:
            continue
        calls.append(call)
        if len(calls) >= limit:
            break
    calls.reverse()
    return calls


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
    excludes the venv bin dir -- caught below, warned to stderr (nothing
    reads it), exit 0. The throttle marker gets touched upstream of this
    call (see ``main``), so the failure looks identical to a healthy tick
    from the outside: "stamp updates, no report ever lands." Measured live
    (session_context_status resolved:false with no row at all despite an
    updating throttle stamp), reproduced by hand.

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
    cli = os.environ.get("AGENT_WAKE_CLI", "").strip()
    if not cli:
        return dict(os.environ)
    solet_dir = str(Path(cli).parent)
    if not (Path(solet_dir) / "solet").is_file():
        return dict(os.environ)
    env = dict(os.environ)
    env["PATH"] = f"{env.get('PATH', '')}:{solet_dir}"
    return env


def _solet_call(process_key: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["solet", "call", process_key, json.dumps(arguments)],
            capture_output=True, text=True, timeout=20, check=False,
            env=_solet_call_env(),
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


def _ancestor_claude_pid() -> int | None:
    """The pid of the nearest ancestor ``claude`` process, or ``None``.

    A hook runs as a detached subprocess of the session it is observing, so
    walking up the process tree is how it finds the session's own pid — the
    same method the ``rename`` skill uses to locate a seat's session file.
    """
    pid = os.getpid()
    for _ in range(_ANCESTOR_WALK_MAX_DEPTH):
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _warn(f"ancestor walk failed at pid {pid}: {exc}")
            return None
        fields = result.stdout.split()
        if result.returncode != 0 or len(fields) < 2:
            return None
        if Path(fields[-1]).name == _CLAUDE_PROCESS_NAME:
            return pid
        try:
            pid = int(fields[0])
        except ValueError:
            return None
        if pid <= 1:
            return None
    return None


def _registry_row_is_fresh(row: dict[str, Any]) -> bool:
    """True iff the row's ``updated_at`` is within the staleness bound.

    ``updated_at`` is stored UTC-NAIVE (measured 2026-08-16: the value is a UTC
    wall clock carrying no timezone suffix). Comparing it against a naive local
    ``datetime.now()`` is wrong by the ENTIRE UTC offset — 25,200s on this host,
    two orders of magnitude larger than the bound below. That does not degrade
    the check, it REPLACES it, silently, in one of two directions: every age
    goes negative and nothing is ever stale (fails open), or an ``abs()`` makes
    everything stale forever and the seat never reports (fails closed — the
    very gap this fallback exists to close). So both sides are UTC-aware here,
    and a wildly negative age is treated as a clock/timezone fault rather than
    as freshness.
    """
    raw = str(row.get("updated_at") or "")
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        _warn(f"registry row has unparseable updated_at {raw!r} -- skipping")
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - stamp).total_seconds()
    if age_seconds < -_SEAT_REGISTRY_MAX_AGE_SECONDS:
        _warn(
            f"registry row is {-age_seconds:.0f}s in the FUTURE -- clock or "
            "timezone fault, not freshness; skipping rather than trusting it",
        )
        return False
    if age_seconds > _SEAT_REGISTRY_MAX_AGE_SECONDS:
        _warn(
            f"registry row is {age_seconds:.0f}s old (bound "
            f"{_SEAT_REGISTRY_MAX_AGE_SECONDS}s) -- a re-used pid may be "
            "matching a dead session; skipping rather than reporting under it",
        )
        return False
    return True


def _registry_rows_for_pid(pid: int) -> list[dict[str, Any]] | None:
    """Registry rows whose ``parent_pid`` matches, or ``None`` if the lookup
    itself failed (which is different from "no session has that pid", and the
    caller must not conflate them)."""
    envelope = _solet_call(_PEER_LIST_PROCESS_KEY, {})
    if envelope is None:
        return None
    instances = (
        (envelope.get("result") or {}).get("data", {}).get("instances") or {}
    )
    return [
        row
        for rows in instances.values()
        for row in rows
        if row.get("parent_pid") == pid
    ]


def _resolve_seat_instance_id() -> str | None:
    """The operator seat's OWN registered instance id, or ``None`` to skip.

    SEAT ONLY, and the scoping is load-bearing rather than incidental. This is
    called only from the branch where ``AGENT_INSTANCE_ID`` is ABSENT. For a
    watch-transport WORKER the registry returns the ``agi-watch-*`` id, not the
    ledger id, so running this fleet-wide would report context under the wrong
    identity for every such worker. The env var is present for exactly those
    sessions, so the gate and the hazard line up — DELIBERATELY, and the
    negative-control test pins it.

    The id is READ from the registry, never minted, and never cached: resolved
    per tick and used for that tick only.
    """
    pid = _ancestor_claude_pid()
    if pid is None:
        _warn("could not resolve an ancestor claude pid -- skipping")
        return None
    matches = _registry_rows_for_pid(pid)
    if matches is None:
        return None
    # 0/1/N: exactly one, or refuse. Reporting context under the wrong identity
    # is worse than not reporting, so an ambiguous match is never guessed. A
    # host driver that leaves parent_pid unpopulated lands here as 0 matches,
    # and skipping IS the correct behaviour for it.
    if len(matches) != 1:
        _warn(
            f"registry has {len(matches)} rows for ancestor pid {pid}, need "
            "exactly 1 -- skipping",
        )
        return None
    if not _registry_row_is_fresh(matches[0]):
        return None
    instance_id = str(matches[0].get("agent_instance_id") or "").strip()
    if not instance_id:
        _warn(f"registry row for pid {pid} carries no agent_instance_id -- skipping")
        return None
    return instance_id


def _resolve_instance_id() -> str:
    """This session's own instance id: env first, seat registry second, else "".

    Kept as its own function so :func:`_resolve_firing_context` carries exactly
    the one branch it always carried -- adding the seat path inline pushed it
    from clean to CC C(11) against the gate's ceiling of 10, and the allowlist
    is tracked debt for pre-existing code, not a bypass for new code.
    """
    env_id = os.environ.get(_INSTANCE_ID_ENV, "").strip()
    if env_id:
        return env_id
    return _identity_without_env() or ""


def _identity_without_env() -> str | None:
    """Identity for a session whose env carries no ``AGENT_INSTANCE_ID``.

    SEAT PATH (2026-08-16). An absent env id used to mean "not fleet-managed;
    skip", which is true for an unmanaged shell and WRONG for the operator
    seat: the seat's identity is resolved dynamically through its live bridge
    and is never baked into its process env, so this hook self-selected out on
    every tick and the seat had no context gauge at all. That is the gap behind
    the 2026-08-16 rotation at 559K against a >300K policy.

    Split out of :func:`_resolve_firing_context` to keep that function's branch
    count under the complexity gate -- and because the seat path deserves to be
    readable on its own rather than as a clause inside an env check.
    """
    resolved = _resolve_seat_instance_id()
    if resolved:
        return resolved
    _warn(
        f"{_INSTANCE_ID_ENV} not set and no registry row resolved -- not a "
        "fleet-managed spawn, or identity is unavailable; skipping",
    )
    return None


def _resolve_firing_context() -> tuple[str, str, str, str] | None:
    """``(marker_dir, agent_instance_id, transcript_path, claude_session_id)``,
    or ``None`` when this firing should be skipped (missing env, unreadable
    stdin, or a payload missing the fields this hook needs) -- split out of
    :func:`main` to keep it a straight-line dispatcher (radon cc)."""
    marker_dir = os.environ.get(_MARKER_DIR_ENV, "").strip()
    agent_instance_id = _resolve_instance_id()
    if not agent_instance_id:
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


def _cache_arguments(
    transcript_path: str, thresholds: Any,
) -> dict[str, Any]:
    """The three optional cache fields, or ``{}`` when they cannot be measured.

    ``{}`` is deliberate and load-bearing: OMITTING the fields records NOT
    REPORTED, which the read-back verb surfaces as ``null``. Sending
    ``cache_cold=False`` here instead would assert a warm cache this hook
    never actually classified.
    """
    calls = read_assistant_calls(transcript_path)
    if not calls:
        return {}
    try:
        parsed = [
            thresholds.AssistantCall(
                at=thresholds.parse_transcript_timestamp(c["at"]),
                cache_read_tokens=c["cache_read"],
            )
            for c in calls
        ]
    except (ValueError, KeyError) as exc:
        _warn(f"could not parse transcript timestamps for cache state: {exc}")
        return {}
    state = thresholds.classify_cache_state(
        parsed, datetime.now(UTC), cleared_at=None,
    )
    return {
        "cache_read_tokens": parsed[-1].cache_read_tokens,
        "cache_cold": state.cold,
        "cache_overage_signature": state.overage_signature,
    }


def _reporter_arguments() -> dict[str, Any]:
    """Which COPY of this hook is speaking, on the two axes a reader needs.

    Several copies of this file can be registered on the same event at once
    (see the three-copy note in the module docstring), they serialize on a
    shared throttle marker that records nothing about who claimed it, and the
    stored row keeps only the latest write. So a row is unattributable unless
    the reporter says who it is, and an absent cache field cannot otherwise be
    told apart from a STALE copy having served that tick.

    SURFACE is derived from this file's own resolved location, as a path
    CLASS -- never the absolute path, which would write one machine's layout
    into shared state. The VENDORED source copy classifies as ``unknown``
    rather than ``plugin_cache``: it is the thing an install copies FROM, not
    a cache copy, and it is only ever executed directly in one measured
    headless configuration. Calling that ``unknown`` is accurate; calling it
    ``plugin_cache`` would be a guess wearing a specific label.

    GENERATION is a local constant, deliberately NOT a git sha -- this file
    cannot know the commit it was copied from, and deriving one would promise
    precision the reporter does not have.
    """
    try:
        parts = Path(__file__).resolve().parts
    except OSError:
        return {"reporter_surface": "unknown", "reporter_generation": _REPORTER_GENERATION}
    return {
        "reporter_surface": _classify_surface(parts),
        "reporter_generation": _REPORTER_GENERATION,
    }


def _has_consecutive(parts: tuple[str, ...], *names: str) -> bool:
    """True when ``names`` appear as CONSECUTIVE path components."""
    width = len(names)
    return any(parts[i : i + width] == names for i in range(len(parts) - width + 1))


def _classify_surface(parts: tuple[str, ...]) -> str:
    """Path CLASS for this copy of the hook. ORDER IS LOAD-BEARING.

    The layouts NEST, so the tests must run most-specific-first:
    a deployed release tree CONTAINS a ``coordination-hooks/hooks/``
    directory, so testing ``vendored`` before ``release`` would label every
    release copy as vendored and silently merge two distinct surfaces --
    which is the same collapsing failure this whole change exists to undo.
    The smoke asserts the ordering by swapping these two tests; if that
    mutation does not go red, the order is not actually being tested.

    Release detection is STRUCTURAL (a ``releases`` component followed by a
    ``rel-*`` component) rather than an absolute prefix. Hardcoding one
    machine's release root into a shipped hook would be the same mistake as
    writing an absolute path into the stored row.

    ``checkout`` matches ``.claude/hooks`` ANYWHERE in the path, not only at
    the tail: hooks live in SUBDIRECTORIES too (``memory_passthrough/``), and
    the original tail-anchored test reported those as ``unknown`` -- a false
    unrecognised-surface for an entirely ordinary checkout file, and one of
    the three things the collapsed bucket was hiding.
    """
    if _has_consecutive(parts, ".claude", "plugins", "cache"):
        return "plugin_cache"
    for index, name in enumerate(parts):
        if name == "releases" and any(p.startswith("rel-") for p in parts[index + 1 :]):
            return "release"
    if _has_consecutive(parts, "coordination-hooks", "hooks"):
        return "vendored"
    if _has_consecutive(parts, ".claude", "hooks"):
        return "checkout"
    return "unknown"


def _report_context_status(
    *, agent_instance_id: str, claude_session_id: str, model: str,
    current_tokens: int, ceiling: int, cache_arguments: dict[str, Any],
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
            **cache_arguments,
            **_reporter_arguments(),
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
        cache_arguments=_cache_arguments(transcript_path, rotation_thresholds),
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
