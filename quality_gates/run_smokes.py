#!/usr/bin/env python3
"""Run the gate-eligible smoke suite listed in ``gate_smokes.txt``.

Project policy is no pytest: smokes are standalone scripts with an
``if __name__ == "__main__"`` entry point that exit 0 on pass, the dedicated
SKIP code on a disclosed, non-blocking dependency gap (see ``_SKIP_EXIT_CODE``
below), and any other non-zero on genuine fail. This runner executes each
smoke named in the tracked register ``quality_gates/gate_smokes.txt`` (one
repo-relative path per line; ``#`` comments and blank lines ignored), with a
per-smoke timeout, and aggregates the verdicts. It is the behavioral half of
the commit gate — the static gates live in ``code_quality_check.py``; this
runs the smokes.

The register is a tracked, growing allowlist (it mirrors the god-class / radon
allowlists): a smoke gates only once it is listed here, so adding a smoke to the
gate is an explicit, reviewable act and the gate never silently broadens to
smokes that need a live platform — with one ratified exception class (GTE-09,
Coordinator-Day Q2): a smoke whose load-bearing proof genuinely requires a live
external dependency may register if it fails LOUD on an unreachable dependency
(see quality_gates/gate_smokes.txt's header for the full statement).

Skip visibility (2026-08-08, undeclared-dependency audit follow-on:
workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md):
before this, a smoke that self-skipped on a missing OPTIONAL tool (printing
"SKIP" and returning normally, exit 0) was indistinguishable from a smoke
that genuinely ran and passed — this runner determined pass/fail purely from
the process exit code and never surfaced a passing smoke's own stdout, so
the skip text was captured and silently discarded. The aggregate "N/M
passed" figure was therefore an upper bound on real coverage, not a
statement of it. A smoke now signals a disclosed, non-blocking skip by
exiting with ``_SKIP_EXIT_CODE`` (77) instead of 0 — the reserved SKIP exit
code from the automake/Meson/CTest test-tooling convention, chosen
deliberately for that portability precedent rather than invented fresh, and
verified against every currently gate-registered smoke's own exit codes to
carry zero collision risk (none use anything but 0/1 today). REPORTING a
skip and treating it as FATAL are separate concerns: this runner always
reports passed/skipped/failed counts distinctly; whether a skip should fail
the *suite* is caller policy via ``--fail-on-skip`` (default: tolerant — the
normal commit gate accepts a disclosed skip; a future hermeticity/seal check
against a born clone, where a skip IS the hollow-gate condition, passes
``--fail-on-skip`` to make it fatal there without this runner needing two
copies of itself).

Exit codes:
  0 - every listed smoke passed (skips, if any, did not trip --fail-on-skip)
  1 - one or more smokes failed, timed out, is a missing path, or (only with
      --fail-on-skip) one or more smokes skipped

Usage:
  .venv/bin/python3 quality_gates/run_smokes.py [--register PATH] [--timeout S] [--list] [--fail-on-skip]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path
from typing import Literal

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_REGISTER = Path(__file__).resolve().parent / "gate_smokes.txt"
_DEFAULT_TIMEOUT = 120

# The automake/Meson/CTest SKIP_RETURN_CODE convention — a smoke exits this
# code to report a disclosed, non-blocking dependency gap. Chosen for that
# portability precedent, not invented fresh. See the module docstring's
# "Skip visibility" section for the full rationale and the collision check.
_SKIP_EXIT_CODE = 77

_Verdict = Literal["passed", "skipped", "failed"]


def _venv_python() -> Path:
    """Return the repo venv interpreter, failing loud if it is absent."""
    candidate = _REPO_ROOT / ".venv" / "bin" / "python3"
    if not candidate.exists():
        raise FileNotFoundError(f"repo venv interpreter not found: {candidate}")
    return candidate


def _require_solet_name() -> str:
    """Fail closed ONCE, here, when ``SOLET_NAME`` is unset.

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
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "SOLET_NAME env var is required -- it is this solet's "
            "database name (database per solet, named after it). The "
            "driving agent must export it before running the smoke suite: "
            "SOLET_NAME=<name> .venv/bin/python3 quality_gates/run_smokes.py"
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


def _run_one(python: Path, smoke: Path, timeout: int) -> tuple[_Verdict, str]:
    """Run a single smoke; return (verdict, captured_output).

    Verdict is "skipped" ONLY on the dedicated ``_SKIP_EXIT_CODE`` — every
    other non-zero code (including a timeout) is "failed", never silently
    folded into "skipped". A smoke choosing to exit 77 for reasons other than
    a genuine disclosed gap is a smoke misusing the convention, not something
    this runner can or should second-guess.
    """
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
        return "failed", f"TIMEOUT after {timeout}s"
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return "passed", output
    if proc.returncode == _SKIP_EXIT_CODE:
        return "skipped", output
    return "failed", output


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the gate-eligible smoke suite.")
    parser.add_argument("--register", type=Path, default=_DEFAULT_REGISTER)
    parser.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT)
    parser.add_argument("--list", action="store_true", help="List the register and exit.")
    parser.add_argument(
        "--fail-on-skip",
        action="store_true",
        help=(
            "Treat any skipped smoke as a suite failure. Off by default (the "
            "commit gate tolerates a disclosed skip); a hermeticity/seal check "
            "against a born clone should pass this, since a skip there IS the "
            "hollow-gate condition being checked for."
        ),
    )
    return parser.parse_args()


def _run_suite(
    python: Path, entries: list[str], timeout: int
) -> tuple[list[str], list[str], list[str]]:
    """Run every smoke in ``entries``; return ``(skipped, failures, missing)`` path lists."""
    skipped: list[str] = []
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
        verdict, output = _run_one(python, smoke, timeout)
        elapsed = time.monotonic() - start
        label = {"passed": "ok", "skipped": "SKIP", "failed": "FAIL"}[verdict]
        print(f"  {label:7} {entry}  ({elapsed:.1f}s)")
        if verdict == "skipped":
            skipped.append(entry)
            tail = "\n    ".join(output.strip().splitlines()[-12:])
            print(f"    {tail}")
        elif verdict == "failed":
            failures.append(entry)
            tail = "\n    ".join(output.strip().splitlines()[-12:])
            print(f"    {tail}")
    return skipped, failures, missing


def _summarize(
    total: int,
    skipped: list[str],
    failures: list[str],
    missing: list[str],
    *,
    fail_on_skip: bool,
) -> int:
    """Print the aggregate verdict; return the process exit code.

    Skips are ALWAYS reported distinctly from passes — never folded into the
    passed count. Whether they trip the exit code is the one policy knob
    (``fail_on_skip``); everything else about the reporting is unconditional.
    """
    passed = total - len(skipped) - len(failures) - len(missing)
    print(f"\nsmokes: {passed}/{total} passed, {len(skipped)}/{total} skipped")
    if skipped:
        print(f"skipped: {len(skipped)} ({', '.join(skipped)})")
    if missing:
        print(f"missing: {len(missing)} ({', '.join(missing)})")
    if failures:
        print(f"failed: {len(failures)} ({', '.join(failures)})")
    blocking_skips = fail_on_skip and bool(skipped)
    if blocking_skips:
        print("--fail-on-skip: skips above are treated as failures for this run.")
    return 1 if (failures or missing or blocking_skips) else 0


def main() -> int:
    args = _parse_args()
    entries = _read_register(args.register)
    if args.list:
        print("\n".join(entries))
        return 0
    _require_solet_name()
    skipped, failures, missing = _run_suite(_venv_python(), entries, args.timeout)
    return _summarize(
        len(entries), skipped, failures, missing, fail_on_skip=args.fail_on_skip,
    )


if __name__ == "__main__":
    raise SystemExit(main())
