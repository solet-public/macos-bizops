"""Shared external-tool runner for the deterministic scanners.

Centralizes subprocess invocation, version capture, and tool-availability
detection so no scanner reimplements it. A tool that is not installed is a
*coverage gap* (surfaced in the run's ``coverage_gaps``), not a masked failure:
the scanner's job is to report what it could and could not examine.

Note (shared gotcha): ``timeout`` is not installed on this Mac — we bound runs
with ``subprocess.run(timeout=...)`` instead of wrapping the command.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

_DEFAULT_TIMEOUT_S = 900


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Result of one external-tool invocation."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def tool_available(name: str) -> bool:
    """True when ``name`` resolves on PATH."""
    return shutil.which(name) is not None


def tool_version(name: str, version_arg: str = "--version") -> str | None:
    """First line of the tool's version banner, or None if it cannot be read."""
    if not tool_available(name):
        return None
    outcome = run([name, version_arg], timeout_s=30)
    line = outcome.stdout.strip() or outcome.stderr.strip()
    return line.splitlines()[0] if line else None


def run(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    env_overrides: dict[str, str] | None = None,
    raise_on_timeout: bool = True,
) -> ToolOutcome:
    """Run ``argv`` and capture output.

    Fast-fail policy: a missing executable, a non-zero exit, or a security
    finding are all legitimate observations the caller interprets — this helper
    does not swallow them. It raises on the two conditions that mean the scan
    itself is broken (executable absent, or it hung past the bound), so the
    failure is loud rather than silently producing an empty finding set.

    ``raise_on_timeout=False`` is for network-dependent tools (semgrep registry,
    pip-audit advisory DB) where a timeout is an *environmental* condition the
    caller converts to a coverage gap, not a defect: the timeout returns a
    ``ToolOutcome`` with ``timed_out=True`` instead of raising.

    ``env_overrides`` are layered onto the current environment (e.g. the platform
    gates want ``HOMUNCULUS_NAME`` set).
    """
    env: dict[str, str] | None = None
    if env_overrides is not None:
        env = dict(os.environ)
        env.update(env_overrides)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
            # Non-interactive tools never read stdin; without this they inherit
            # the parent's (piped) stdin and an interactive-ish tool (e.g. a
            # `claude -p` reviewer subprocess) blocks forever waiting on it.
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"tool not found on PATH: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        if raise_on_timeout:
            raise RuntimeError(f"tool timed out after {timeout_s}s: {' '.join(argv)}") from exc
        return ToolOutcome(returncode=-1, stdout="", stderr=f"timed out after {timeout_s}s", timed_out=True)
    return ToolOutcome(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
