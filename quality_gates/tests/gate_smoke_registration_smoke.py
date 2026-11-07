#!/usr/bin/env python3
"""Regression for the gate-smoke registration completeness census.

The negative control creates an actual untracked ``*_smoke.py`` in a temporary
Git repository.  The census must reject it until a distinct, tagged exclusion
is added; merely having a gate register is not enough.

FRAGMENT ROUTE (prb_ae6d81ea).  A smoke may be registered either in
``gate_smokes.txt`` or in a fragment file of the author's own under
``gate_smokes.d/`` beside it.  ``test_fragment_registration_is_accepted`` is the
pass-path test for the one refusal this census owns: a fragment-registered
smoke must be reported as registered, so an author satisfies the gate by adding
one line to their own file instead of editing the shared register.  The
refusal itself is unchanged -- a genuinely unregistered smoke is still caught,
which the test asserts in the same breath.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quality_gates.gate_smoke_registration_gate import inspect_registration  # noqa: E402

_TODAY = date(2026, 9, 20)
_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        _FAILURES.append(name)


def _write(path: Path, text: str = "def main() -> int:\n    return 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def test_fragment_registration_is_accepted() -> None:
    """A smoke registered ONLY in a fragment is registered, and a miss is still caught."""
    with _fixture() as tmp:
        root = Path(tmp)
        fragment_only = "quality_gates/tests/fragment_registered_smoke.py"
        _write(root / fragment_only)
        # Written to the author's own file, not to the shared register.
        _write(
            root / "quality_gates/gate_smokes.d/one-candidate.txt",
            f"# one candidate's own registration\n{fragment_only}\n",
        )
        _git(root, "add", ".")
        report = inspect_registration(root, today=_TODAY)
        check("fragment-registered smoke is accepted as registered", report.unregistered == ())
        check("fragment entry is in the registered set", fragment_only in report.registered)
        check("fragment registration invents no stale exclusion", report.stale_exclusions == ())
        check("fragment registration invents no overlap", report.overlaps == ())

        # A SECOND candidate's fragment does not collide with the first.
        second = "quality_gates/tests/second_fragment_smoke.py"
        _write(root / second)
        _write(root / "quality_gates/gate_smokes.d/another-candidate.txt", f"{second}\n")
        _git(root, "add", ".")
        both = inspect_registration(root, today=_TODAY)
        check("two candidates register through two files with no shared path", both.unregistered == ())

        # The refusal is preserved: an unregistered smoke is still caught even
        # though fragments exist.
        forgotten = "quality_gates/tests/forgotten_with_fragments_smoke.py"
        _write(root / forgotten)
        still = inspect_registration(root, today=_TODAY)
        check("unregistered smoke is still caught alongside fragments", still.unregistered == (forgotten,))


def test_unreadable_fragment_is_advisory_not_a_blocking_finding() -> None:
    """A damaged fragment must not turn a registered smoke into a blocking miss."""
    with _fixture() as tmp:
        root = Path(tmp)
        fragment_only = "quality_gates/tests/advisory_fragment_smoke.py"
        _write(root / fragment_only)
        _write(root / "quality_gates/gate_smokes.d/readable.txt", f"{fragment_only}\n")
        damaged = root / "quality_gates/gate_smokes.d/damaged.txt"
        damaged.parent.mkdir(parents=True, exist_ok=True)
        damaged.write_bytes(b"\xff\xfe not utf-8\n")
        _git(root, "add", ".")
        report = inspect_registration(root, today=_TODAY)
        check("a damaged fragment does not block the readable registrations", report.unregistered == ())
        check("the readable fragment's entry is still registered", fragment_only in report.registered)


def _fixture() -> tempfile.TemporaryDirectory[str]:
    fixture = tempfile.TemporaryDirectory(prefix="gate_smoke_registration_")
    root = Path(fixture.name)
    _git(root, "init", "-q")
    _write(root / "quality_gates/tests/registered_smoke.py")
    _write(
        root / "quality_gates/gate_smokes.txt",
        "quality_gates/tests/registered_smoke.py\n",
    )
    _write(
        root / "quality_gates/gate_smoke_exclusions.txt",
        "# Exact deliberately-excluded smoke paths.\n",
    )
    _git(root, "add", ".")
    return fixture


def test_registered_and_excluded_partition_is_clean() -> None:
    with _fixture() as tmp:
        report = inspect_registration(Path(tmp), today=_TODAY)
    check("registered smoke is accepted", report.unregistered == ())
    check("no stale exclusion exists", report.stale_exclusions == ())


def test_new_untracked_smoke_fails_until_explicitly_excluded() -> None:
    with _fixture() as tmp:
        root = Path(tmp)
        forgotten = "quality_gates/tests/forgotten_smoke.py"
        _write(root / forgotten)
        before = inspect_registration(root, today=_TODAY)
        check("temporary untracked smoke is caught", before.unregistered == (forgotten,))

        exclusions = root / "quality_gates/gate_smoke_exclusions.txt"
        exclusions.write_text(
            exclusions.read_text(encoding="utf-8") + f"{forgotten}  # owner: iss_fb75bf89 reason: fixture-only deliberate exclusion expires: 2026-12-31\n",
            encoding="utf-8",
        )
        after = inspect_registration(root, today=_TODAY)
        check("explicit tagged exclusion clears only the named smoke", after.unregistered == ())
        check("new exclusion is not stale", after.stale_exclusions == ())


def test_overlap_and_stale_exclusion_fail_closed() -> None:
    with _fixture() as tmp:
        root = Path(tmp)
        exclusions = root / "quality_gates/gate_smoke_exclusions.txt"
        exclusions.write_text(
            "quality_gates/tests/registered_smoke.py  # owner: iss_fb75bf89 reason: fixture overlap expires: 2026-12-31\nquality_gates/tests/removed_smoke.py  # owner: iss_fb75bf89 reason: fixture stale entry expires: 2026-12-31\n",
            encoding="utf-8",
        )
        report = inspect_registration(root, today=_TODAY)
    check("registered/excluded overlap is reported", report.overlaps == ("quality_gates/tests/registered_smoke.py",))
    check("removed smoke exclusion is stale", report.stale_exclusions == ("quality_gates/tests/removed_smoke.py",))


def main() -> int:
    print("Gate-smoke registration completeness smoke\n")
    test_registered_and_excluded_partition_is_clean()
    test_new_untracked_smoke_fails_until_explicitly_excluded()
    test_overlap_and_stale_exclusion_fail_closed()
    test_fragment_registration_is_accepted()
    test_unreadable_fragment_is_advisory_not_a_blocking_finding()
    if _FAILURES:
        print(f"\nFAILED: {_FAILURES}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
