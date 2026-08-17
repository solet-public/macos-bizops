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

Resolving the CLI is only half the contract; the resolved path also has to
SURVIVE. Both rungs below land inside a versioned release directory, and a
deploy reaps old releases — so on 2026-08-16/17 every long-running worker's
``AGENT_WAKE_CLI``, ``PATH`` prepend, and ``solet watch`` sidecar went
dangling together on cutover, silently, for the same non-fatal-by-design
reason. :func:`stable_release_path` expresses the resolved path through the
deployment's atomically-swapped ``current`` pointer so a spawn survives the
next deploy; :func:`resolve_solet_bin` applies it once, for every surface.

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

RELEASE_ID_PREFIX = "rel-"
"""Basename prefix of a materialized release directory. Mirrors
``macos_self_deployment_plugin.release_manager.RELEASE_ID_PREFIX``; duplicated
rather than imported because this module is stdlib-only by contract (the
console script and the standalone bridge subprocess both import it bare) and
must not take a plugin dependency to name a directory."""

CURRENT_LINK_NAME = "current"
"""The deployment's stable pointer, swapped atomically on cutover. Mirrors
``release_manager.CURRENT_LINK_NAME``; duplicated for the same reason."""


def _pointer_names_release(pointer: Path, release_dir: Path) -> bool:
    """True when ``pointer`` (a symlink) CURRENTLY names ``release_dir``.

    Compares the raw link text — ``os.readlink`` plus ``normpath``, handling
    both the relative target the release manager writes and an absolute one —
    and deliberately never ``Path.resolve()``: resolving is the trap that
    would collapse the stable pointer back onto the versioned directory and
    make the whole rewrite decorative.

    A missing/unreadable link reads as "no match" rather than raising. This is
    a narrow, named race window, not a swallowed error: the pointer is
    replaced by ``rename`` during a cutover, so a spawn landing in that window
    must fall back to the versioned path it already had — raising here would
    break spawns that work today, which the Repair-4 precedent forbids.
    """
    try:
        target = os.readlink(pointer)
    except OSError:
        return False
    joined = os.path.normpath(os.path.join(str(pointer.parent), target))
    return joined == os.path.normpath(str(release_dir))


def stable_release_path(binary_path: str) -> str:
    """Rewrite a VERSIONED release path onto the deployment's stable pointer.

    ``~/.ananta/releases/<name>/rel-<ts>-<sha>/venv/bin/solet`` becomes
    ``~/.ananta/releases/<name>/current/venv/bin/solet`` — the same file
    today, and still a valid file after the next cutover swaps ``current``.

    This is the fix for the systemic defect measured 2026-08-16/17: a spawned
    worker's ``AGENT_WAKE_CLI``, its ``PATH`` prepend, and its ``solet watch``
    registration sidecar were all pinned into a versioned directory, and a
    deploy REAPS old releases (``release_manager`` keeps the last K), so every
    deploy dangled every long-running worker's CLI at once — silently, because
    each consumer is deliberately non-fatal.

    Conservative by construction. The rewrite happens only when the pointer
    currently names the very release the path came from, and only when the
    rewritten path actually stats as an executable file; anything else returns
    ``binary_path`` UNCHANGED. Two consequences worth stating:

    * A path outside a release layout (a developer checkout, an operator's
      ``/usr/local/bin/solet``, a test's temp dir) is returned untouched — no
      release layout is assumed to exist anywhere.
    * Mid-cutover skew, where ``current`` already names a DIFFERENT release
      than the running process, is REFUSED rather than rewritten. Redirecting
      a worker onto a version its spawner is not running would be a silent
      version substitution; keeping the versioned path is honest and is
      exactly the pre-fix behavior.

    The non-regression the Repair-4 guard established still holds either way:
    a dangling export contributes nothing anywhere, so a session that would
    resolve ``solet`` from PATH is never made worse by this.
    """
    if not binary_path:
        return binary_path
    source = Path(binary_path)
    if not source.is_absolute():
        return binary_path
    for release_dir in source.parents:
        if not release_dir.name.startswith(RELEASE_ID_PREFIX):
            continue
        pointer = release_dir.parent / CURRENT_LINK_NAME
        if not pointer.is_symlink():
            continue
        if not _pointer_names_release(pointer, release_dir):
            continue
        stable = pointer / source.relative_to(release_dir)
        if stable.is_file() and os.access(stable, os.X_OK):
            return str(stable)
    return binary_path


def _discover_solet_bin(
    explicit: str | None,
    python_executable: str | None,
) -> str:
    """PATH first, then the active Python environment's sibling console
    script. Split out of :func:`resolve_solet_bin` so the discovery rungs and
    the release-pointer stabilization stay independently readable."""
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


def resolve_solet_bin(
    explicit: str | None,
    *,
    python_executable: str | None = None,
) -> str:
    """Resolve the CLI from PATH, then from the active Python environment,
    then express the result through the stable release pointer when there is
    one.

    Materialized blue-green releases intentionally run with a minimal PATH
    that does not include their own ``venv/bin`` directory.  The release's
    Python executable and ``solet`` console script are siblings, so that
    directory is the deterministic second rung when PATH lookup is empty.

    Both rungs (and an ``explicit`` override) hand back a path inside a
    VERSIONED release directory, which the next deploy may reap. Every spawn
    surface derives from this one function — ``expose_worker_cli``'s
    ``AGENT_WAKE_CLI`` and ``PATH`` prepend, ``watch_sidecar_argv``'s
    registration sidecar, ``worker_path``, and the Codex config overrides —
    so :func:`stable_release_path` is applied HERE, once, rather than at six
    call sites that would drift apart.

    Returns ``""`` when neither rung resolves — callers decide whether that is
    fatal (the Codex watch path refuses to spawn) or merely degrading (the
    Claude adapters fall back to the bare command name, preserving their
    pre-fix behavior exactly rather than refusing a spawn that used to work).
    """
    return stable_release_path(
        _discover_solet_bin(explicit, python_executable),
    )


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
    "CURRENT_LINK_NAME",
    "RELEASE_ID_PREFIX",
    "expose_worker_cli",
    "resolve_solet_bin",
    "stable_release_path",
    "watch_sidecar_argv",
    "worker_path",
]
