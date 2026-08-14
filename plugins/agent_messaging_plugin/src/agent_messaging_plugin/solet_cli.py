"""Runner-neutral resolution and exposure of the ``solet`` CLI for spawned
fleet workers.

Every spawned worker — Claude Code or Codex, tmux-hosted or subprocess-hosted
— needs the CLI to be BOTH resolvable as a command and reachable at an
absolute path:

* ``heartbeat_report_alive.py`` (PostToolUse) shells out to a bare
  ``["solet", "call", ...]``;
* ``wake_waiter.py`` (Stop) runs ``[$AGENT_WAKE_CLI, "wake", ...]``;
* the ``/rename`` skill and the watch sidecar both invoke the CLI directly.

A materialized blue-green release, and a tmux pane inheriting the tmux
SERVER's environment rather than the spawning process's, both run with a
minimal ``PATH`` that excludes the release's own ``venv/bin``. Every one of
the consumers above then fails with ``FileNotFoundError``, and — because each
is deliberately non-fatal (portability posture, reviewed coordination-hooks
surface) — fails SILENTLY: exit 0, no liveness stamp, no wake, no error
anywhere. That is the registration-loss mechanism measured on 2026-08-14
(``workbench/2026-08-13_registration_loss_rca_lane_r_report.md``).

These helpers were introduced for the managed-Codex path (bed25c2a8,
52edfb559, 74cd36af6, all 2026-08-13) and lived in ``codex_common``; they are
not Codex-specific in any way, and the Claude adapters needed exactly the same
treatment. Hoisted here so both runners share ONE implementation rather than
the asymmetry that left every spawned Claude worker unregistered.
``codex_common`` re-exports them under its established private names, so no
Codex call site or smoke changes.

stdlib-only, matching ``env_contract``'s constraint: the console script and
the standalone bridge subprocess must both be able to import it bare.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

CLI_COMMAND_NAME = "solet"
"""The wake CLI's own BINARY name — never the solet INSTANCE name
(``SOLET_NAME``, e.g. ``acme``). Conflating the two is the 2026-08-08
deaf-wake defect: ``which <instance-name>`` fails, ``which solet`` resolves."""


def resolve_solet_bin(
    explicit: str | None,
    *,
    python_executable: str | None = None,
) -> str:
    """Resolve the CLI from PATH, then from the active Python environment.

    Materialized blue-green releases intentionally run with a minimal PATH
    that does not include their own ``venv/bin`` directory.  The release's
    Python executable and ``solet`` console script are siblings, so that
    directory is the deterministic second rung when PATH lookup is empty.

    Returns ``""`` when neither rung resolves — callers decide whether that is
    fatal (the Codex watch path refuses to spawn) or merely degrading (the
    Claude adapters fall back to the bare command name, preserving their
    pre-fix behavior exactly rather than refusing a spawn that used to work).
    """
    if explicit is not None:
        return explicit
    discovered = shutil.which(CLI_COMMAND_NAME)
    if discovered:
        return discovered
    executable = Path(python_executable or sys.executable)
    sibling = executable.parent / CLI_COMMAND_NAME
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return ""


def expose_worker_cli(env: dict[str, str], solet_bin: str) -> None:
    """Expose the resolved CLI to hooks and ordinary worker shell commands.

    Sets ``AGENT_WAKE_CLI`` to the ABSOLUTE binary (so the Stop hook's
    ``subprocess.run([$AGENT_WAKE_CLI, ...])`` never depends on PATH at all)
    AND prepends its directory to ``PATH`` (so the hooks and skills that
    invoke a bare ``solet`` — which this fix deliberately does not rewrite —
    resolve it too). Mutates ``env`` in place.
    """
    env["AGENT_WAKE_CLI"] = solet_bin or CLI_COMMAND_NAME
    if not solet_bin or not Path(solet_bin).is_absolute():
        return
    cli_dir = str(Path(solet_bin).parent)
    path_entries = env.get("PATH", "").split(os.pathsep)
    if cli_dir in path_entries:
        return
    env["PATH"] = os.pathsep.join(
        [cli_dir, *[entry for entry in path_entries if entry]],
    )


def worker_path(solet_bin: str) -> str:
    """The PATH a managed worker should run with, CLI directory first."""
    env = {"PATH": os.environ.get("PATH", "")}
    expose_worker_cli(env, solet_bin)
    return env["PATH"]


def watch_sidecar_argv(
    solet_bin: str, *, agent_id: str, spool: bool,
) -> list[str]:
    """The registered-presence sidecar's argv, minus ``--exit-with-parent``
    (each host supplies its own parent pid: a literal for a tracked
    subprocess, shell-expanded ``$$`` for a tmux pane).

    ``--no-claim`` ALWAYS. Registration is presence, not role ownership:
    ``spawn_session.role_name`` authorizes a later model-driven claim but must
    never bind a role as a launcher side effect
    (``local_cli.cli._register_without_claim``). It also keeps the
    claim-under-inherited-session-id trap out of the launcher path entirely.

    ``spool`` is the ONE axis where the runners legitimately differ, so it is
    a required keyword rather than a default anyone can drift into:

    * Claude Code -> ``True``. ``wake_waiter.py`` is a real async Stop hook
      and the spool tee is precisely what it consumes; arming without it
      would re-deafen the wake this sidecar exists to enable.
    * stock Codex -> ``False``. Async command hooks do not execute there, so
      the tee would only accumulate an unread file for the pane's lifetime
      (codex-0147-dead-spool-retirement, 2026-08-13).
    """
    argv = [solet_bin, "watch", "--agent-id", agent_id, "--no-claim"]
    if not spool:
        argv.append("--no-spool")
    return argv


__all__ = [
    "CLI_COMMAND_NAME",
    "expose_worker_cli",
    "resolve_solet_bin",
    "watch_sidecar_argv",
    "worker_path",
]
