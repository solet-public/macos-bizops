"""Hermetic smoke for the L1 deterministic scanners.

Exercises the pure-Python scanners (no external tool needed) on crafted fixtures
and asserts they emit the right F1 dimension/constraint — plus that a tool-absent
path records a coverage gap rather than silently passing. Run via the gate-smoke
runner (``quality_gates/run_smokes.py``) or directly with
``.venv/bin/python3 plugins/code_vetting_plugin/tests/l1_scanners_smoke.py``
(exit 0/1); the ``code_vetting_plugin`` package must be installed
(``pip install -e plugins/code_vetting_plugin``). Also runnable as pytest
``test_*`` functions.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from code_vetting_plugin.models import Dimension
from code_vetting_plugin.scanners import deps, duplication, hidden_unicode, secrets
from code_vetting_plugin.targets import TargetTree

# The fixture's hidden char is built from an escape so this source file carries no
# literal zero-width character (which the hidden-unicode scanner would rightly flag).
_ZERO_WIDTH_SPACE = chr(0x200B)
_DUP_BLOCK = "\n".join(f"    value_{i} = compute_step({i}, base, offset, scale)" for i in range(12))


def _make_tree(root: Path) -> TargetTree:
    tracked: list[str] = []

    danger = root / "docs" / "danger.md"
    danger.parent.mkdir(parents=True, exist_ok=True)
    danger.write_text(f"visible{_ZERO_WIDTH_SPACE}hidden zero-width here\n", encoding="utf-8")
    tracked.append("docs/danger.md")

    manifest = root / "plugins" / "foo_plugin" / "pyproject.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('[project]\nname = "foo_plugin"\nversion = "0.1.0"\n', encoding="utf-8")
    tracked.append("plugins/foo_plugin/pyproject.toml")

    # A non-two-bucket SPDX with no blacklisted substring — must still be flagged
    # (the license gate is a pure allowlist, not a leaky substring denylist).
    bar = root / "plugins" / "bar_plugin" / "pyproject.toml"
    bar.parent.mkdir(parents=True, exist_ok=True)
    bar.write_text('[project]\nname = "bar_plugin"\nlicense = "MPL-2.0"\n', encoding="utf-8")
    tracked.append("plugins/bar_plugin/pyproject.toml")

    for name in ("alpha", "beta"):
        module = root / "plugins" / "foo_plugin" / "src" / "foo_plugin" / f"{name}.py"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(f"def {name}(base, offset, scale):\n{_DUP_BLOCK}\n    return value_0\n", encoding="utf-8")
        tracked.append(f"plugins/foo_plugin/src/foo_plugin/{name}.py")

    return TargetTree(root=root, tracked=tuple(sorted(tracked)))


def test_hidden_unicode_flags_zero_width() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = hidden_unicode.scan(_make_tree(Path(tmp)), "rid")
    hits = [f for f in result.findings if f.dimension is Dimension.HIDDEN_UNICODE]
    assert hits, "expected a hidden-unicode finding for the zero-width char"
    assert any(f.constraint_violated == "hidden_unicode:zero_width" for f in hits)


def test_license_flags_missing_spdx() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = deps.scan_licenses(_make_tree(Path(tmp)), "rid")
    assert any(
        f.dimension is Dimension.LICENSE and f.constraint_violated == "RB-LICENSE:missing-spdx"
        for f in result.findings
    ), "expected a missing-SPDX finding for the license-less plugin manifest"


def test_license_flags_non_two_bucket_spdx() -> None:
    # Regression guard: a non-two-bucket SPDX (MPL-2.0) with no blacklisted
    # substring must be flagged — the gate is an allowlist, not a denylist.
    with tempfile.TemporaryDirectory() as tmp:
        result = deps.scan_licenses(_make_tree(Path(tmp)), "rid")
    assert any(
        f.constraint_violated == "RB-LICENSE:non-two-bucket-spdx" for f in result.findings
    ), "expected MPL-2.0 to be flagged as a non-two-bucket SPDX"


def test_duplication_flags_exact_block() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = duplication.scan(_make_tree(Path(tmp)), "rid")
    assert any(f.dimension is Dimension.DUP for f in result.findings), "expected a duplicate-block finding"


def _never_available(_name: str) -> bool:
    return False


def test_absent_tool_records_coverage_gap() -> None:
    # Hermetic tool-absence: force the resolution seam to "not installed" instead of
    # asserting host state — installing trufflehog on the host must not red this smoke
    # (it did on 2026-07-21 when the R7 tool provisioning landed the binary).
    real_tool_available = secrets.tool_available
    secrets.tool_available = _never_available
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = secrets.scan_trufflehog(_make_tree(Path(tmp)), "rid")
    finally:
        secrets.tool_available = real_tool_available
    assert not result.coverage.ran
    assert result.coverage.gap_reason is not None
    assert not result.findings


def main() -> int:
    tests = [
        test_hidden_unicode_flags_zero_width,
        test_license_flags_missing_spdx,
        test_license_flags_non_two_bucket_spdx,
        test_duplication_flags_exact_block,
        test_absent_tool_records_coverage_gap,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OK: {len(tests)} L1 scanner smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
