"""The exact check of a tmux host session, as a leaf module.

``session_hosts`` imports both tmux drivers when it loads, so anything the
drivers import must not import the plugin back: this module holds tmux
knowledge and standard-library imports only.

``tmux has-session -t <ref>`` is a prefix match; ``-t =<ref>`` is exact. A
notice that a host session closed is a trigger, never proof, so the check is
exact: a person's session of a longer name must never read as the record's
host session.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_run = subprocess.run
"""The one subprocess seam; tests patch it."""

_GONE_PREFIXES = ("can't find session", "no server running on")
_GONE_SUFFIX = "(No such file or directory)"
NOTICE_PROCESS_KEY = "plugin::agent_messaging_plugin::session_host_closed"
logger = logging.getLogger(__name__)


def notify_on_exit(
    wait: Callable[[], object],
    *,
    host: str,
    host_ref: str,
    solet_name: str,
    solet_bin: str,
) -> threading.Thread | None:
    """Wait for one child exit and send an advisory end trigger."""
    if not solet_name or not solet_bin:
        logger.warning("%s end notice for %s unavailable: missing solet name or CLI", host, host_ref)
        return None

    def report_exit() -> None:
        try:
            returncode = wait()
        except Exception as exc:  # noqa: BLE001 -- thread boundary; no caller can catch this
            logger.warning("%s end notice for %s wait failed: %s", host, host_ref, exc)
            return
        logger.info("%s child %s exited with %s", host, host_ref, returncode)
        try:
            result = _run(
                [solet_bin, "call", "--no-wait", NOTICE_PROCESS_KEY, json.dumps({"host": host, "host_ref": host_ref})],
                env={**os.environ, "SOLET_NAME": solet_name},
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 -- subprocess seam is also a thread boundary
            logger.warning("%s end notice for %s failed: %s", host, host_ref, exc)
            return
        if result.returncode:
            logger.warning("%s end notice for %s exited %s: %s", host, host_ref, result.returncode, result.stderr)

    thread = threading.Thread(target=report_exit, name=f"end-notice-{host}-{host_ref}", daemon=True)
    thread.start()
    return thread


@dataclass(frozen=True, slots=True)
class HostObservation:
    """What one exact host check found: present, gone, or unknown with why."""

    state: Literal["present", "gone", "unknown"]
    detail: str


def tmux_argv() -> list[str]:
    """The tmux command prefix, resolved as the drivers resolve it.

    Change chg_f6cf67d9 (S7) adds the solet's own tmux server here.
    """
    return [shutil.which("tmux") or "tmux"]


def observe_tmux_session(host_ref: str, *, tmux: Sequence[str] | None = None) -> HostObservation:
    """Ask tmux whether the session named exactly ``host_ref`` exists.

    Exit 0 is present. Exit 1 with a "can't find session" or "no server
    running on" message, or one ending "(No such file or directory)", is gone.
    Anything else (another message, another exit code, an OSError, a timeout)
    is unknown: it says the check could not be made, not that the session ended.
    """
    try:
        completed = _run(
            [*(tmux or tmux_argv()), "has-session", "-t", "=" + host_ref],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return HostObservation("unknown", f"{type(exc).__name__}: {exc}")
    stderr = str(completed.stderr or "").strip()
    if completed.returncode == 0:
        return HostObservation("present", stderr)
    detail = stderr or f"has-session exited {completed.returncode}"
    if completed.returncode == 1 and (stderr.startswith(_GONE_PREFIXES) or stderr.endswith(_GONE_SUFFIX)):
        return HostObservation("gone", detail)
    return HostObservation("unknown", detail)


def session_closed_hook_command(solet_name: str, solet_bin: str) -> str:
    """Build the foreground tmux hook whose command detaches the notice call."""
    payload = '"{\\"host\\": \\"tmux\\", \\"host_ref\\": \\"#{q:hook_session_name}\\"}"'
    command = f"(SOLET_NAME={shlex.quote(solet_name)} {shlex.quote(solet_bin)} call --no-wait {NOTICE_PROCESS_KEY} {payload}) </dev/null >/dev/null 2>&1 &"
    return f"run-shell '{command}'"


def _valid_notice_identity(solet_name: str, solet_bin: str) -> bool:
    return bool(solet_name and solet_bin and "'" not in solet_name and "'" not in solet_bin)


def _own_hook_line(stdout: str, solet_name: str) -> str | None:
    marker = f"SOLET_NAME={shlex.quote(solet_name)} "
    return next((line for line in stdout.splitlines() if marker in line and NOTICE_PROCESS_KEY in line), None)


def arm_session_closed_notice(
    *,
    tmux: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess[str]],
    solet_name: str,
    solet_bin: str,
) -> bool:
    """Keep one server-global notice for this solet, advisory on failure."""
    if not _valid_notice_identity(solet_name, solet_bin):
        logger.warning("session-closed notice: invalid solet name or CLI path")
        return False
    hook = session_closed_hook_command(solet_name, solet_bin)
    step = "show-hooks"
    try:
        shown = run_fn([*tmux, "show-hooks", "-g", "session-closed"], capture_output=True, text=True, timeout=5)
        if shown.returncode:
            logger.warning("session-closed notice %s failed: %s", step, shown.stderr)
            return False
        own_line = _own_hook_line(shown.stdout or "", solet_name)
        if own_line is not None:
            if solet_bin in own_line:
                return True
            match = re.match(r"session-closed\[(\d+)\]", own_line)
            if match is None:
                logger.warning("session-closed notice %s returned an unindexed own hook: %s", step, own_line)
                return False
            command = [*tmux, "set-hook", "-g", f"session-closed[{match.group(1)}]", hook]
        else:
            command = [*tmux, "set-hook", "-ga", "session-closed", hook]
        step = "set-hook"
        set_result = run_fn(command, capture_output=True, text=True, timeout=5)
        if set_result.returncode:
            logger.warning("session-closed notice %s failed: %s", step, set_result.stderr)
            return False
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("session-closed notice %s failed: %s", step, exc)
        return False
    return True
