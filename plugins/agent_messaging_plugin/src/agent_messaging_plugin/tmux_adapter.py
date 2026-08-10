"""Fleet session-management Phase B, D2 (§5) — the ``tmux`` HostDriver:
the preferred-where-declared substrate, hardening the R1 spike
(``workbench/2026-08-03_r1_tmux_single_substrate_spike/FINDINGS.md``, MEASURED
GREEN) into production code. Spawns a real, detached tmux session running the
INTERACTIVE ``claude`` CLI (not the ``headless`` driver's stream-json pipe —
a tmux pane is a terminal, so the driver channel injects literal keystrokes,
exactly what a human attaching via ``tmux -CC`` would type). iTerm2 ``-CC``
attach is presentation ONLY (§8 item 2): nothing on this module's spawn/
lifecycle path touches the iTerm2 Python API.

ADAPTER REQUIREMENTS THE SPIKE ESTABLISHES (FINDINGS.md, baked in here):
1. ``allow-passthrough on`` — set globally on the tmux SERVER right after the
   first session this driver creates (a fresh server defaults it off on
   tmux>=3.3; there is no way to read a global option before a server
   exists, so this driver SETS it rather than only checking it).
2. Tag emission must be tmux-aware: the spawned pane emits the DCS-wrapped
   OSC 1337 role tag via ``emit_role_tag.sh`` (the reference impl this
   module ships alongside, per scope item 2) — the raw/unwrapped form is
   confirmed swallowed inside tmux.
3. The role tag is NOT the source of truth — L0 (the ledger) owns identity;
   the terminal var is presentation only. Re-emit-on-attach automation (a
   tmux hook re-invoking the emit on ``client-attached``) is NOT implemented
   in this slice — named as a follow-up in the D2 land report, not silently
   waved off (FINDINGS.md already flags multi-client/re-emit as "not
   measured", exactly the ripeness gap this lane's smokes/land-report
   record, per dispatch-brief scope item 5).
4. Presentation cleanup is this driver's job on the TMUX side: ``terminate``
   always runs ``kill-session`` so no native tmux session lingers. Cleaning
   up a DEAD iTerm2 window left over from a killed tmux session is iTerm2
   Python-API territory — out of scope per dispatch-brief scope item 4
   ("documented, not coded").

VERSION GATE: ``allow-passthrough`` and ``new-session -e`` (per-session
environment injection, used for identity wiring) both require tmux>=3.2/3.3;
:meth:`TmuxHostDriver.verify_config` refuses anything older by name rather
than failing opaquely mid-spawn.

IDENTITY WIRING mirrors ``headless_adapter.py`` exactly (same
``AGENT_INSTANCE_ID``/``AGENT_SESSION_ID`` contract ``backfill_registration``
depends on) — injected via ``tmux new-session -e KEY=VAL`` instead of a
``subprocess.Popen`` env mapping. ``AGENT_SESSION_ID`` is minted exactly ONCE,
here, same as the headless driver's own module docstring warns (two
evaluations of an identity expression is two identities).

SWAP-DURABLE BY CONSTRUCTION (D2 live-acceptance evidence, 2026-08-04
13:07-08Z): a tmux-hosted worker's pane belongs to the independent tmux
daemon, not to this plugin process — nothing on the platform's stop/swap
path touches it, so a tmux-hosted worker survives a blue-green deploy swap
that the headless driver's process-child workers do NOT (``headless_adapter.
py``'s own module docstring names that contrasting property: ``shutdown()``
SIGTERM-then-SIGKILLs every tracked headless worker on platform stop,
unconditionally, including mid-swap).

PROCESS TARGETING: the pane's command is ``sh -c 'exec <claude argv>'`` so the
pane's own pid (``#{pane_pid}``) IS the ``claude`` process, not an
intermediate shell — and, as a fresh tmux pane, that pid is also a process
group leader. :meth:`terminate` signals the WHOLE group (``os.killpg`` via
``_sigterm_then_kill_process_group``, falling back to
``headless_adapter``'s single-pid ``_sigterm_then_kill``/``_pid_alive``
helpers only if ``os.getpgid`` itself fails), not just the leader pid — a
child the claude process backgrounds (e.g. a watch-transport worker's own
``homunculus watch --role X &``, the standard rename-skill onboarding path)
shares the pane's process group and would otherwise survive the leader's
death as a launchd orphan, still holding the worker's role in the peer
registry (measured live, 2026-08-09/10 restart_session run — see
``workbench/2026-08-09_choreography_live_verify_mverbs-impl.md`` §4).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .headless_adapter import (
    WorkerHookResolutionError,
    _authority_system_prompt,
    _pid_alive,
    _resolve_heartbeat_marker_dir,
    _resolve_session_mapping_spool_dir,
    _resolve_worker_hook_paths,
    _sigterm_then_kill,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_ENV_PERMISSION_MODE = "FLEET_HEADLESS_PERMISSION_MODE"
_MIN_TMUX_VERSION = (3, 3)
DEFAULT_TERMINATE_GRACE_SECONDS = 10.0
DEFAULT_PANE_WIDTH = 200
DEFAULT_PANE_HEIGHT = 50
_SESSION_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

# PTY-confirm fix (D3 slice 0a): a tmux-hosted claude launched with
# --dangerously-load-development-channels sits at a real interactive
# confirmation prompt with no CLI bypass flag -- D2 live-acceptance
# evidence (2026-08-04 13:0xZ) confirmed a driver-spawned worker would hang
# here forever, one send-keys Enter clears it. Piped-stdin/headless spawns
# never hit this (the prompt is PTY-only), so the loop below is gated on the
# flag's actual presence in the built claude argv, never assumed.
_DEV_CHANNELS_FLAG = "--dangerously-load-development-channels"
_DEV_CHANNELS_PROMPT_MARKERS = ("WARNING: Loading development channels", "Enter to confirm")
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 20.0
DEFAULT_CONFIRM_POLL_INTERVAL_SECONDS = 0.5


def _needs_dev_channels_confirmation(claude_cmd: list[str]) -> bool:
    """Gate for the bounded PTY-confirm expect loop in :meth:`TmuxHostDriver.
    spawn` -- only a claude argv carrying ``--dangerously-load-development-
    channels`` ever shows the interactive confirmation prompt; an argv
    without the flag must never enter the loop at all, not even for a
    single ``capture-pane`` poll (the no-flag path stays exactly as fast and
    side-effect-free as before this fix)."""
    return _DEV_CHANNELS_FLAG in claude_cmd


def _pane_shows_dev_channels_prompt(pane_text: str) -> bool:
    return any(marker in pane_text for marker in _DEV_CHANNELS_PROMPT_MARKERS)


def _emit_role_tag_path() -> Path:
    return Path(__file__).resolve().parent / "tmux_support" / "emit_role_tag.sh"


def _env_pairs(
    *, agent_instance_id: str, agent_session_id: str, label: str,
    homunculus_name: str, allowed_tools: object, transport: str,
) -> list[str]:
    """``-e KEY=VAL`` args for ``tmux new-session`` — split out of
    :meth:`TmuxHostDriver.spawn` to keep it under the radon cc threshold.
    Same identity-wiring contract as ``headless_adapter._spawn_env``.
    ``transport`` is caller-resolved (fleet-watch-transport-migration phase
    2 slice 1, 2026-08-06) -- never hardcoded here, the same declared,
    never-probed FLEET_TRANSPORT contract every consumer reads
    independently."""
    pairs: list[str] = []
    for key, value in {
        "HOMUNCULUS_NAME": homunculus_name,
        "AGENT_IDENTITY": "claude_code",
        "AGENT_INSTANCE_ID": agent_instance_id,
        "AGENT_SESSION_ID": agent_session_id,
        "AGENT_SESSION_LABEL": label,
        # Deaf-wake fix (2026-08-08): this MUST be the wake CLI's own binary
        # name ("homunculus"), never `homunculus_name` (the homunculus
        # INSTANCE name, e.g. "myhomunculus") -- wake_waiter.py runs
        # `subprocess.run([$AGENT_WAKE_CLI, "wake"])` and the instance name
        # is not a resolvable command (measured: `which <instance-name>` fails, `which
        # homunculus` resolves). The prior value was a currently-masked
        # second defect -- masked because the Stop hook that reads this
        # variable was never wired at all until this same fix; fixing only
        # the wiring without this would silently reintroduce a dead wake.
        "AGENT_WAKE_CLI": "homunculus",
        "FLEET_TRANSPORT": transport,
    }.items():
        pairs += ["-e", f"{key}={value}"]
    if isinstance(allowed_tools, (list, tuple)) and allowed_tools:
        pairs += ["-e", f"FLEET_HEADLESS_TOOL_ALLOWLIST={','.join(str(t) for t in allowed_tools)}"]
    # T1 usage-capture lane (ruling 2026-08-05, Q1(a)): same declared-not-
    # derived contract as headless_adapter._spawn_env -- omitted entirely
    # when APP_HOME is unset.
    spool_dir = _resolve_session_mapping_spool_dir()
    if spool_dir is not None:
        pairs += ["-e", f"ANANTA_SESSION_MAPPING_SPOOL_DIR={spool_dir}"]
    # T2 heartbeat lane (seat's redesign ruling 2026-08-05): same
    # declared-not-derived, conditional-export contract.
    heartbeat_dir = _resolve_heartbeat_marker_dir()
    if heartbeat_dir is not None:
        pairs += ["-e", f"AGENT_HEARTBEAT_MARKER_DIR={heartbeat_dir}"]
    return pairs


def _pane_command(claude_cmd: list[str], *, label: str) -> str:
    """The tag emit MUST run and complete BEFORE ``exec`` replaces this
    shell with the claude process — once exec'd, the pane has no shell left
    to receive a follow-up command; anything sent after that point is raw
    input to whatever is running (a first version of :meth:`TmuxHostDriver.
    spawn` had exactly this bug: a post-spawn ``send-keys`` landed as
    garbage input to the just-exec'd process instead of running as a
    command)."""
    emit_script = _emit_role_tag_path()
    emit_prefix = (
        f"sh {shlex.quote(str(emit_script))} {shlex.quote(label)}; "
        if emit_script.exists() else ""
    )
    return f"{emit_prefix}exec {shlex.join(claude_cmd)}"


def _sanitize_session_name(raw: str) -> str:
    """tmux session names may not contain ``:`` or ``.`` (both are
    target-syntax separators); collapse anything else non-shell-friendly
    too, so a lane_id with slashes/spaces never breaks ``-t`` targeting."""
    return _SESSION_NAME_UNSAFE.sub("-", raw) or "session"


def _parse_tmux_version(version_output: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", version_output)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _resolve_str(explicit: str | None, env_key: str, *, default: str = "") -> str:
    """``explicit if explicit is not None else os.environ.get(env_key) or
    default`` — factored out so :meth:`TmuxHostDriver.__init__` reads as a
    flat list of field assignments instead of N inline ternaries (each one
    is a branch radon's cyclomatic-complexity count charges to the
    function). ``is not None``, never ``or``: an explicit empty string is
    "explicitly blank", distinct from "not provided" (mirrors
    ``headless_adapter``'s own contract)."""
    if explicit is not None:
        return explicit
    return os.environ.get(env_key) or default


def _resolve_bin(explicit: str | None, which_name: str, default: str) -> str:
    """Same ``is not None`` contract as :func:`_resolve_str`, but the
    fallback is ``shutil.which`` instead of an env var (the ``tmux_bin``/
    ``claude_bin`` shape)."""
    if explicit is not None:
        return explicit
    return shutil.which(which_name) or default


DEFAULT_PASTE_STABLE_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_PASTE_STABLE_SAMPLES_REQUIRED = 3
DEFAULT_PASTE_STABLE_TIMEOUT_SECONDS = 10.0
"""Same conservative constants as the already-proven iTerm2 precedent
(``seat_rotation_helper.py``'s ``wait_for_screen_stable``, rotation-
systematization fix loop #2) — inherited as a starting default, not
independently recalibrated for tmux's own render timing beyond the live
measurement in ``workbench/2026-08-08_spawn_registration_gaps_findings_
rotation-impl.md`` (a real charter-sized payload's stranding was visible
within ~2s and stayed stable, never resolving on its own — consistent with
tmux's local paste-chunking completing fast once ``send-keys -l`` returns).
Flagged as an open item if further tuning is ever warranted; not treated as
precisely measured for tmux specifically the way the iTerm2 defaults were."""


class _TmuxSendKeysDriverChannel:
    """The ``DriverChannel`` (§5) for a tmux-hosted pane — injects literal
    keystrokes (``send-keys -l``) followed by a separate ``Enter`` key press,
    exactly what a human attaching to the same pane would type. ``-l``
    (literal) is load-bearing: without it, tmux interprets key-name-shaped
    text (e.g. a message that happens to read ``Enter`` or ``C-c``) as a key
    binding instead of literal characters.

    **Paste-stability wait (spawn/registration-gaps fix, 2026-08-08,
    live-measured — see the findings file section named above):** a large
    literal payload sent via ``send-keys -l`` can get chunked by tmux into
    multiple separate bracketed-paste events on the receiving Claude Code
    TUI (measured: a ~9.7KB payload split into 9 ``[Pasted text #N]``
    fragments), and an ``Enter`` sent immediately afterward — the OLD,
    unguarded behavior — lands while that multi-chunk paste is still being
    assembled/rendered and gets absorbed as a literal newline inside it
    instead of submitting. Same failure shape as the already-fixed iTerm2
    ``async_send_text`` race (rotation-systematization fix loop #2);
    independently reproduced here for tmux with the real production code,
    not merely inferred from the parallel. The fix mirrors that one exactly:
    poll the pane's own rendered content (``tmux capture-pane -p``) until it
    stops changing across N consecutive samples, THEN send the submitting
    ``Enter`` — never immediately after the text send."""

    def __init__(
        self, *, tmux_bin: str, session: str, run_fn: Any,
        poll_interval_seconds: float = DEFAULT_PASTE_STABLE_POLL_INTERVAL_SECONDS,
        stable_samples_required: int = DEFAULT_PASTE_STABLE_SAMPLES_REQUIRED,
        stable_timeout_seconds: float = DEFAULT_PASTE_STABLE_TIMEOUT_SECONDS,
        sleep_fn: Any = time.sleep,
        now_fn: Any = time.monotonic,
    ) -> None:
        self._tmux_bin = tmux_bin
        self._session = session
        self._run_fn = run_fn
        self._poll_interval_seconds = poll_interval_seconds
        self._stable_samples_required = stable_samples_required
        self._stable_timeout_seconds = stable_timeout_seconds
        self._sleep_fn = sleep_fn
        self._now_fn = now_fn

    def send(self, text: str) -> None:
        try:
            self._run_fn(
                [self._tmux_bin, "send-keys", "-t", self._session, "-l", "--", text],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Fire-and-forget by contract (session_hosts.DriverChannel) — a
            # pane that died between driver_channel()'s liveness check and
            # this write (TOCTOU) gets a swallowed, logged send rather than
            # an unmapped exception escaping through the verb layer. Never
            # attempts the Enter if the text send itself already failed.
            logger.warning(
                "tmux driver channel send() failed — session %r is no longer reachable",
                self._session,
            )
            return
        self._wait_for_paste_stable()
        try:
            self._run_fn(
                [self._tmux_bin, "send-keys", "-t", self._session, "Enter"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning(
                "tmux driver channel send() failed sending Enter — session %r is no "
                "longer reachable",
                self._session,
            )

    def _capture_pane(self) -> str | None:
        """The pane's own rendered content, or ``None`` on any capture
        failure — treated as "not stable yet" by the caller, never as a
        crash (fire-and-forget contract, same as :meth:`send` itself)."""
        try:
            result = self._run_fn(
                [self._tmux_bin, "capture-pane", "-t", self._session, "-p"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        return str(getattr(result, "stdout", "") or "")

    def _wait_for_paste_stable(self) -> None:
        """Poll until the pane's rendered content is IDENTICAL across
        ``stable_samples_required`` consecutive samples, matching
        ``wait_for_screen_stable``'s exact "identical to the immediately
        preceding sample" semantics — there is no target signature to check
        against (the payload's post-render form isn't known in advance,
        same reasoning as the iTerm2 precedent). Fails OPEN on timeout
        (logs loudly, still sends the Enter) rather than closed: this
        channel's own established contract is fire-and-forget best-effort,
        never silently doing nothing — a timeout is no worse than today's
        unguarded immediate-Enter behavior, and better whenever the pane
        happens to settle just past the window."""
        deadline = self._now_fn() + self._stable_timeout_seconds
        prev: str | None = None
        stable_count = 0
        while True:
            if self._now_fn() > deadline:
                logger.warning(
                    "tmux driver channel: pane %r did not stabilize within %.1fs before "
                    "the submitting Enter — sending it anyway (fail-open; the alternative "
                    "is never submitting at all).",
                    self._session, self._stable_timeout_seconds,
                )
                return
            current = self._capture_pane()
            if current is not None and current == prev:
                stable_count += 1
                if stable_count >= self._stable_samples_required:
                    return
            else:
                stable_count = 0
            prev = current
            self._sleep_fn(self._poll_interval_seconds)


def _sigterm_then_kill_process_group(pgid: int, grace_seconds: float) -> None:
    """SIGTERM then SIGKILL a whole process group — mirrors
    ``headless_adapter._sigterm_then_kill``'s shape but targets ``pgid`` via
    ``os.killpg`` instead of a single pid via ``os.kill``, so a pane's own
    backgrounded children die with it. No ``Popen`` handle to reap here (the
    tmux daemon owns the process, not this Python process), so liveness is
    always the raw ``kill -0`` probe."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.error("SIGTERM denied for tmux pane process group pgid=%d", pgid)
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pgid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.error("SIGKILL denied for tmux pane process group pgid=%d", pgid)


class TmuxHostDriver:
    """The ``tmux`` driver (§5): preferred-where-declared substrate. One
    detached tmux session per :meth:`spawn` call, running the interactive
    ``claude`` CLI; driven via literal keystrokes over ``send-keys``."""

    def __init__(
        self,
        *,
        tmux_bin: str | None = None,
        claude_bin: str | None = None,
        homunculus_name: str | None = None,
        permission_mode: str | None = None,
        transport: str | None = None,
        mcp_config_path: Path | None = None,
        cwd: Path | None = None,
        run_fn: Any = subprocess.run,
        grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
        pane_width: int = DEFAULT_PANE_WIDTH,
        pane_height: int = DEFAULT_PANE_HEIGHT,
        confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT_SECONDS,
        confirm_poll_interval_seconds: float = DEFAULT_CONFIRM_POLL_INTERVAL_SECONDS,
        sleep_fn: Any = time.sleep,
    ) -> None:
        from .headless_adapter import (
            _resolve_default_cwd,  # noqa: PLC0415 -- avoid module-load cycle risk
        )

        self._tmux_bin = _resolve_bin(tmux_bin, "tmux", "tmux")
        self._claude_bin = _resolve_bin(
            claude_bin, "claude", str(Path.home() / ".local" / "bin" / "claude"),
        )
        self._homunculus_name = _resolve_str(homunculus_name, "HOMUNCULUS_NAME")
        self._permission_mode = _resolve_str(permission_mode, _ENV_PERMISSION_MODE)
        # fleet-watch-transport-migration phase 2 slice 1 (2026-08-06):
        # mirrors headless_adapter.HeadlessHostDriver's own floor exactly --
        # spec-level (spawn_session's policy resolution) always wins; never
        # a fail-closed gate (unlike permission_mode), so an unset floor
        # resolves to the charter's own default ("watch") in _spawn_command
        # rather than refusing to spawn.
        self._transport = transport if transport is not None else ""
        self._cwd = cwd if cwd is not None else _resolve_default_cwd()
        self._mcp_config_path = (
            mcp_config_path if mcp_config_path is not None else (self._cwd / ".mcp.json")
        )
        self._run_fn = run_fn
        self._grace_seconds = grace_seconds
        self._pane_width = pane_width
        self._pane_height = pane_height
        self._confirm_timeout_seconds = confirm_timeout_seconds
        self._confirm_poll_interval_seconds = confirm_poll_interval_seconds
        self._sleep_fn = sleep_fn

    def _tmux_binary_remedies(self) -> list[str]:
        """Split out of :meth:`verify_config` to keep it under the radon cc
        threshold — the tmux-specific half (binary presence + version)."""
        remedies: list[str] = []
        resolved_tmux = shutil.which(self._tmux_bin) or (
            self._tmux_bin if os.path.isabs(self._tmux_bin) else None
        )
        if not (resolved_tmux and os.access(resolved_tmux, os.X_OK)):
            remedies.append(
                f"no executable 'tmux' binary found (checked PATH, then "
                f"{self._tmux_bin!r}) — install tmux (e.g. 'brew install tmux') "
                "or pass tmux_bin explicitly.",
            )
            return remedies
        try:
            version_check = self._run_fn(
                [resolved_tmux, "-V"], capture_output=True, text=True, timeout=5,
            )
            version_stdout = version_check.stdout or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            version_stdout = ""
            logger.warning("tmux -V raised while checking version: %s", exc)
        parsed = _parse_tmux_version(version_stdout)
        if parsed is None or parsed < _MIN_TMUX_VERSION:
            remedies.append(
                f"tmux version {version_stdout.strip()!r} is older than "
                f"{_MIN_TMUX_VERSION[0]}.{_MIN_TMUX_VERSION[1]} (or unparseable/unreachable) — "
                "'allow-passthrough' and per-session '-e' env injection both "
                "require it; upgrade tmux.",
            )
        return remedies

    def _claude_launch_remedies(self, *, permission_mode: str | None) -> list[str]:
        """Split out of :meth:`verify_config` to keep it under the radon cc
        threshold — the same underlying-``claude``-launch checks
        :meth:`HeadlessHostDriver.verify_config` makes, since a tmux-hosted
        pane still launches a real ``claude`` process."""
        remedies: list[str] = []
        if not (self._claude_bin and os.access(self._claude_bin, os.X_OK)):
            remedies.append(
                f"no executable 'claude' binary found (checked PATH via "
                f"shutil.which, then {self._claude_bin!r}) — install Claude "
                "Code or pass claude_bin explicitly.",
            )
        if not self._homunculus_name:
            remedies.append(
                "HOMUNCULUS_NAME is not set — the spawned session cannot "
                "discover its bridge port without it.",
            )
        if not (permission_mode or self._permission_mode):
            remedies.append(
                f"no permission mode configured (neither a per-spawn override nor "
                f"{_ENV_PERMISSION_MODE} is set) — an unattended tmux-hosted worker "
                "needs an explicit operator-ruled posture; this driver never "
                "defaults to bypass.",
            )
        if not self._mcp_config_path.exists():
            remedies.append(f"no MCP config found at {self._mcp_config_path}")
        return remedies

    def verify_config(self, *, permission_mode: str | None = None) -> list[str]:
        """Config-time, fail-closed remedies (§5) — empty means ready to
        spawn."""
        return [
            *self._tmux_binary_remedies(),
            *self._claude_launch_remedies(permission_mode=permission_mode),
        ]

    def capability_report(self) -> dict[str, object]:
        return {
            "host": "tmux",
            "topology": "detached-session",
            "inspectable_via": ["tmux attach -t <host_ref>", "tmux -CC attach -t <host_ref>"],
            "attach_hint": (
                "detached tmux session; attach directly or via iTerm2 'tmux -CC attach "
                "-t <host_ref>' for native-window presentation (documented separately, "
                "not automated by this driver)"
            ),
        }

    def _spawn_command(self, spec: Mapping[str, object], *, transport: str) -> list[str]:
        permission_mode = str(spec.get("permission_mode") or "") or self._permission_mode
        # R4 Package C (2026-08-10): resolved via the two-rung ladder
        # (origin checkout, then the plugin's shipped fallback copy) --
        # see headless_adapter._resolve_worker_hook_paths's own docstring.
        # Raises WorkerHookResolutionError, converted to
        # HostCannotSpawnError by the caller (spawn()), if any file
        # resolves at neither rung -- never silently emits settings
        # pointing at a missing path.
        resolved_hooks = _resolve_worker_hook_paths(self._cwd)
        allowlist_hook_path = resolved_hooks["headless_tool_allowlist_gate.py"]
        capture_hook_path = resolved_hooks["capture_session_mapping.py"]
        heartbeat_hook_path = resolved_hooks["heartbeat_report_alive.py"]
        rotation_due_hook_path = resolved_hooks["rotation_due_watch.py"]
        # Deaf-wake fix (2026-08-08): project-vendored Python ports of
        # coordination-hooks@<homunculus>'s four JS hooks, wired here (the adapter's
        # own generated --settings blob) rather than the shared project-
        # scope .claude/settings.json, specifically so this does not
        # double-fire the wake for the seat -- the seat already gets these
        # via the user-scope plugin, which --setting-sources project
        # excludes for a spawned worker in the first place (that exclusion
        # is the root cause this fix addresses; see
        # workbench/2026-08-08_deaf_wake_diagnosis_findings_rotation-impl.md).
        wake_hook_path = resolved_hooks["wake_waiter.py"]
        check_messages_hook_path = resolved_hooks["check_messages_reminder.py"]
        step_zero_hook_path = resolved_hooks["step_zero_reminder.py"]
        role_binding_hook_path = resolved_hooks["role_binding_reminder.py"]
        import json  # noqa: PLC0415 -- kept local, mirrors headless_adapter's own inline usage

        # Agent/Task tool deny (capability-tier guardrail redesign, fleet-
        # watch-transport-migration phase 2 slice 1+5, 2026-08-06): carried
        # here, not just in this checkout's own project-scope
        # .claude/settings.json, because --setting-sources project resolves
        # relative to whatever cwd the spawned worker actually runs in -- a
        # non-checkout spawn would not inherit this origin's tracked
        # settings file. See headless_adapter.py._hook_settings_json's own
        # docstring for the live scratch-probe evidence this mirrors.
        settings = {
            "permissions": {
                "deny": ["Agent", "Task"],
            },
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": f"python3 {allowlist_hook_path}"}]},
                ],
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": f"python3 {capture_hook_path}"}]},
                    {
                        # Matches coordination-hooks@<homunculus>'s own hooks.json
                        # matcher for this event exactly (fidelity, not a
                        # new choice) -- check_messages_reminder.py also
                        # fires here (in addition to UserPromptSubmit
                        # below) matching the plugin's own dual wiring.
                        "matcher": "startup|resume|clear",
                        "hooks": [
                            {"type": "command", "command": f"python3 {check_messages_hook_path}"},
                            {"type": "command", "command": f"python3 {role_binding_hook_path}"},
                        ],
                    },
                ],
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": f"python3 {step_zero_hook_path}"}]},
                    {"hooks": [{"type": "command", "command": f"python3 {check_messages_hook_path}"}]},
                ],
                "PostToolUse": [
                    {"hooks": [
                        {"type": "command", "command": f"python3 {heartbeat_hook_path}"},
                        {"type": "command", "command": f"python3 {rotation_due_hook_path}"},
                    ]},
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {wake_hook_path}",
                                # Matches wake_waiter.js's own required
                                # shape exactly -- without this the Stop
                                # hook applies Claude Code's short default
                                # timeout instead of blocking indefinitely
                                # for a peer-message wake.
                                "asyncRewake": True,
                                "timeout": 86400,
                            },
                        ],
                    },
                ],
            },
        }
        cmd = [
            self._claude_bin,
            "--permission-mode", permission_mode,
            "--setting-sources", "project",
            "--settings", json.dumps(settings),
        ]
        # fleet-watch-transport-migration phase 2 slice 1 (2026-08-06):
        # mirrors headless_adapter.py's own posture exactly -- "mcp" gets
        # the real bridge config, "watch" gets an EXPLICIT empty one
        # (WS-6-verified precedent), never omitted (omitting risks ambient
        # .mcp.json re-attachment). Dev-channel loading stays unconditional
        # -- orthogonal to MCP-vs-watch, a separate mechanism per the
        # phase-2 scope ruling.
        if transport == "mcp":
            cmd += ["--mcp-config", str(self._mcp_config_path), "--strict-mcp-config"]
        else:
            cmd += ["--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
        cmd += ["--dangerously-load-development-channels", f"server:{self._homunculus_name}"]
        model = str(spec.get("model") or "")
        if model:
            cmd += ["--model", model]
        effort = str(spec.get("effort") or "")
        if effort:
            cmd += ["--effort", effort]
        # T2 authority-template (seat's design ruling 2026-08-05): same
        # ON-by-default contract as the headless adapter.
        cmd += ["--append-system-prompt", _authority_system_prompt(spec)]
        return cmd

    def _resolve_transport(self, spec: Mapping[str, object]) -> str:
        """fleet-watch-transport-migration phase 2 slice 1 (2026-08-06):
        mirrors headless_adapter.HeadlessHostDriver._resolve_transport
        exactly. Split out of :meth:`spawn` to keep it under the radon cc
        threshold."""
        return str(spec.get("transport") or "") or self._transport or "watch"

    def spawn(self, spec: Mapping[str, object]) -> str:
        # Deferred: session_hosts imports THIS module to populate its
        # driver registry — a module-level import here would be circular.
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        remedies = self.verify_config(permission_mode=str(spec.get("permission_mode") or ""))
        if remedies:
            raise HostCannotSpawnError("; ".join(remedies))
        agent_instance_id = str(spec.get("agent_instance_id") or "")
        if not agent_instance_id:
            raise HostCannotSpawnError(
                "spawn spec is missing agent_instance_id — the tmux driver "
                "cannot register the spawned session under the ledger's "
                "identity without it.",
            )
        label = str(spec.get("lane_id") or "") or agent_instance_id
        session_name = _sanitize_session_name(f"fleet-{label}-{agent_instance_id[-8:]}")
        # Minted exactly ONCE, here — never re-derived elsewhere (two
        # evaluations of an identity expression is two identities).
        agent_session_id = f"ases-{agent_instance_id}"
        # Resolved ONCE, here, threaded into both _env_pairs (FLEET_TRANSPORT)
        # and _spawn_command (the MCP-config argv posture).
        transport = self._resolve_transport(spec)

        env_pairs = _env_pairs(
            agent_instance_id=agent_instance_id, agent_session_id=agent_session_id,
            label=label, homunculus_name=self._homunculus_name,
            allowed_tools=spec.get("allowed_tools") or (), transport=transport,
        )
        try:
            claude_cmd = self._spawn_command(spec, transport=transport)
        except WorkerHookResolutionError as exc:
            raise HostCannotSpawnError(str(exc)) from exc
        pane_command = _pane_command(claude_cmd, label=label)
        new_session_cmd = [
            self._tmux_bin, "new-session", "-d", "-s", session_name,
            "-x", str(self._pane_width), "-y", str(self._pane_height),
            *env_pairs,
            "-c", str(self._cwd),
            "sh", "-c", pane_command,
        ]
        self._launch_new_session(new_session_cmd)
        # allow-passthrough is a SERVER-global option with no meaning until a
        # server exists — set it right after this call guarantees one, per
        # FINDINGS requirement 1, rather than depending on pre-existing
        # server state (which spawn's own verify_config cannot observe).
        self._run_fn(
            [self._tmux_bin, "set", "-g", "allow-passthrough", "on"],
            capture_output=True, text=True, timeout=5,
        )
        if _needs_dev_channels_confirmation(claude_cmd):
            self._confirm_dev_channels_prompt(session_name)
        return session_name

    def _launch_new_session(self, new_session_cmd: list[str]) -> None:
        """Split out of :meth:`spawn` to keep it under the radon cc
        threshold -- the ``tmux new-session`` subprocess call and its two
        failure shapes (raised exception, non-zero exit)."""
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        try:
            result = self._run_fn(
                new_session_cmd, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostCannotSpawnError(f"tmux new-session raised: {exc}") from exc
        if result.returncode != 0:
            raise HostCannotSpawnError(
                f"tmux new-session failed (exit {result.returncode}): {result.stderr.strip()}",
            )

    def _capture_pane_text(self, session_name: str) -> str:
        try:
            result = self._run_fn(
                [self._tmux_bin, "capture-pane", "-t", session_name, "-p"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout or ""

    def _await_pane_condition(self, session_name: str, predicate: Any) -> bool:
        """Bounded poll loop shared by both stages of the PTY-confirm expect
        sequence -- true the moment ``predicate`` first matches captured pane
        text, false once ``confirm_timeout_seconds`` elapses with no match."""
        deadline = time.monotonic() + self._confirm_timeout_seconds
        while True:
            if predicate(self._capture_pane_text(session_name)):
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep_fn(self._confirm_poll_interval_seconds)

    def _confirm_dev_channels_prompt(self, session_name: str) -> None:
        """The bounded expect loop (D3 slice 0a): wait for the interactive
        ``--dangerously-load-development-channels`` confirmation to appear on
        the pane, clear it with a literal Enter, then wait for it to actually
        clear before trusting the spawn. A real tmux-hosted claude sits at
        this prompt forever with no CLI bypass flag (D2 live-acceptance
        evidence, 2026-08-04 13:0xZ) -- returning early here would hand back
        a session id for a pane that never actually reaches the agent."""
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        if not self._await_pane_condition(session_name, _pane_shows_dev_channels_prompt):
            self._kill_session(session_name)
            raise HostCannotSpawnError(
                f"tmux pane {session_name!r} never showed the "
                "--dangerously-load-development-channels confirmation prompt "
                f"within {self._confirm_timeout_seconds}s -- killed the "
                "half-alive session instead of returning one that may hang "
                "forever.",
            )
        self._run_fn(
            [self._tmux_bin, "send-keys", "-t", session_name, "Enter"],
            capture_output=True, text=True, timeout=10,
        )
        cleared = self._await_pane_condition(
            session_name, lambda text: not _pane_shows_dev_channels_prompt(text),
        )
        if not cleared:
            self._kill_session(session_name)
            raise HostCannotSpawnError(
                f"tmux pane {session_name!r} still showed the "
                "development-channels confirmation prompt "
                f"{self._confirm_timeout_seconds}s after Enter was sent -- "
                "killed the half-alive session instead of returning one "
                "stuck at the prompt.",
            )

    def alive(self, host_ref: str) -> bool:
        try:
            result = self._run_fn(
                [self._tmux_bin, "has-session", "-t", host_ref],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _pane_pid(self, host_ref: str) -> int | None:
        try:
            result = self._run_fn(
                [self._tmux_bin, "list-panes", "-t", host_ref, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        first_line = (result.stdout or "").strip().splitlines()
        if not first_line:
            return None
        try:
            return int(first_line[0])
        except ValueError:
            return None

    def _kill_session(self, host_ref: str) -> None:
        """Native-session cleanup on teardown (FINDINGS requirement 4, tmux
        side) — idempotent: a session already gone is success, not an
        error, matching ``terminate_session``'s own idempotent contract."""
        with_suppress = self._run_fn(
            [self._tmux_bin, "kill-session", "-t", host_ref],
            capture_output=True, text=True, timeout=5,
        )
        del with_suppress  # non-zero here just means "already gone" — not fatal

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        grace = grace_seconds if grace_seconds > 0 else self._grace_seconds
        pane_pid = self._pane_pid(host_ref)
        if pane_pid is not None and _pid_alive(pane_pid):
            # Signal the pane's WHOLE process group, not just pane_pid: a
            # tmux pane is a fresh process-group leader, and any child the
            # pane's own claude process backgrounds (e.g. a watch-transport
            # worker's `homunculus watch --role X &`, the standard
            # rename-skill onboarding path for every non-MCP spawn) inherits
            # that same group. Signaling pane_pid alone leaves such a child
            # orphaned under launchd, still holding the worker's role in the
            # peer registry — peer_holds_role then reports a dead role as
            # healthy (measured live, 2026-08-09/10 restart_session run).
            try:
                pgid = os.getpgid(pane_pid)
            except ProcessLookupError:
                pgid = None
            if pgid is not None:
                _sigterm_then_kill_process_group(pgid, grace)
            else:
                _sigterm_then_kill(pane_pid, None, grace)
        # Kill the tmux session regardless of whether a pane pid was found —
        # this is the ONE unconditional native-cleanup step (FINDINGS
        # requirement 4): it must run even if the pid-level signal path
        # found nothing (a session with no pane, or a race).
        self._kill_session(host_ref)

    def driver_channel(self, host_ref: str) -> _TmuxSendKeysDriverChannel | None:
        if not self.alive(host_ref):
            return None
        return _TmuxSendKeysDriverChannel(
            tmux_bin=self._tmux_bin, session=host_ref, run_fn=self._run_fn,
        )


__all__ = [
    "DEFAULT_TERMINATE_GRACE_SECONDS",
    "TmuxHostDriver",
]
