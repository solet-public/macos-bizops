#!/usr/bin/env python3
"""Run the gate-eligible smoke suite listed in ``gate_smokes.txt``.

Project policy is no pytest: smokes are standalone scripts with an
``if __name__ == "__main__"`` entry point that exit 0 on pass and non-zero on
fail. This runner executes each smoke named in the tracked register
``quality_gates/gate_smokes.txt`` (one repo-relative path per line; ``#``
comments and blank lines ignored), with a per-smoke timeout, and aggregates the
verdicts. It is the behavioral half of the commit gate — the static gates live
in ``code_quality_check.py``; this runs the smokes.

The register is a tracked, growing allowlist (it mirrors the god-class / radon
allowlists): a smoke gates only once it is listed here, so adding a smoke to the
gate is an explicit, reviewable act and the gate never silently broadens to
smokes that need a live platform — with one ratified exception class (GTE-09,
Coordinator-Day Q2): a smoke whose load-bearing proof genuinely requires a live
external dependency may register if it fails LOUD on an unreachable dependency
(see quality_gates/gate_smokes.txt's header for the full statement).

Exit codes:
  0 - every listed smoke passed
  1 - one or more smokes failed, timed out, or is a missing path

Usage:
  .venv/bin/python3 quality_gates/run_smokes.py [--register PATH] [--timeout S] [--list]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_REGISTER = Path(__file__).resolve().parent / "gate_smokes.txt"
_DEFAULT_TIMEOUT = 120


def _venv_python() -> Path:
    """Return the repo venv interpreter, failing loud if it is absent."""
    candidate = _REPO_ROOT / ".venv" / "bin" / "python3"
    if not candidate.exists():
        raise FileNotFoundError(f"repo venv interpreter not found: {candidate}")
    return candidate


def _require_homunculus_name() -> str:
    """Fail closed ONCE, here, when ``HOMUNCULUS_NAME`` is unset.

    Many smokes resolve the platform identity at import time and fail closed
    without it, so an unset variable turns a suite run into a wall of unrelated
    tracebacks whose shared cause is invisible -- the failure signature reads as
    a broken platform rather than a missing export. Checking at the entry point
    turns that into one discoverable message before any smoke is spawned.

    The wording is bootstrap.py's, deliberately: the platform already states
    this requirement there, and a second phrasing of the same requirement is a
    divergence waiting to rot. Presence only -- name VALIDATION belongs at
    genesis (bootstrap.py), and duplicating the pattern here would be the same
    divergence in another form.
    """
    name = os.environ.get("HOMUNCULUS_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "HOMUNCULUS_NAME env var is required -- it is this homunculus's "
            "database name (database per homunculus, named after it). The "
            "driving agent must export it before running the smoke suite: "
            "HOMUNCULUS_NAME=<name> .venv/bin/python3 quality_gates/run_smokes.py"
        )
    return name


def _read_register(register: Path) -> list[str]:
    """Parse the register into a list of repo-relative smoke paths."""
    if not register.exists():
        raise FileNotFoundError(f"gate-smoke register not found: {register}")
    entries: list[str] = []
    for raw in register.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def _run_one(python: Path, smoke: Path, timeout: int) -> tuple[bool, str]:
    """Run a single smoke; return (passed, captured_output)."""
    try:
        proc = subprocess.run(
            [str(python), str(smoke)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s"
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the gate-eligible smoke suite.")
    parser.add_argument("--register", type=Path, default=_DEFAULT_REGISTER)
    parser.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT)
    parser.add_argument("--list", action="store_true", help="List the register and exit.")
    return parser.parse_args()


def _run_suite(
    python: Path, entries: list[str], timeout: int
) -> tuple[list[str], list[str]]:
    """Run every smoke in ``entries``; return ``(failures, missing)`` path lists."""
    failures: list[str] = []
    missing: list[str] = []
    print(f"Running {len(entries)} gate-eligible smokes (timeout {timeout}s each)\n")
    for entry in entries:
        smoke = _REPO_ROOT / entry
        if not smoke.exists():
            missing.append(entry)
            print(f"  MISSING  {entry}")
            continue
        start = time.monotonic()
        passed, output = _run_one(python, smoke, timeout)
        elapsed = time.monotonic() - start
        print(f"  {'ok' if passed else 'FAIL':7} {entry}  ({elapsed:.1f}s)")
        if not passed:
            failures.append(entry)
            tail = "\n    ".join(output.strip().splitlines()[-12:])
            print(f"    {tail}")
    return failures, missing


def _summarize(total: int, failures: list[str], missing: list[str]) -> int:
    """Print the aggregate verdict; return the process exit code."""
    passed = total - len(failures) - len(missing)
    print(f"\nsmokes: {passed}/{total} passed")
    if missing:
        print(f"missing: {len(missing)} ({', '.join(missing)})")
    if failures:
        print(f"failed: {len(failures)} ({', '.join(failures)})")
    return 1 if (failures or missing) else 0


def main() -> int:
    args = _parse_args()
    entries = _read_register(args.register)
    if args.list:
        print("\n".join(entries))
        return 0
    _require_homunculus_name()
    failures, missing = _run_suite(_venv_python(), entries, args.timeout)
    return _summarize(len(entries), failures, missing)


if __name__ == "__main__":
    raise SystemExit(main())
