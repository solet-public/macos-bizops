"""structural_metrics_smoke.py — R8-1: the lizard-backed structural-metrics scanner.

Pins the ruling's contract with EXACT deterministic values (ruling §A/§B/§C/§D/§E):

  * Roster: ONE UNIVERSAL, parse-only (`executes_target_code=False`) scanner; the roster is 18.
  * Pin + languages: lizard is pinned exact (a drift raises); `supported_languages()` includes
    typescript/tsx/javascript/python and EXCLUDES dart (a lizard upgrade that silently changes
    the supported set turns this red instead of drifting foreign coverage unnoticed).
  * EXACT per-function metrics on a TS+Python fixture: CCN/NLOC/params/nesting are deterministic,
    so we assert the VALUES (not shapes) — CCN via lizard's boolean-op counting, nesting depth via
    the `nd` extension, a fresh analyzer per file (no cross-file accumulation).
  * Band findings (universal tier / FOREIGN): CCN>30 → MEDIUM, params>8 / nesting>6 → LOW; a clean
    function crosses nothing; severity NEVER exceeds MEDIUM; dimension is COMPLEXITY and zero-FP-
    promoted. On a SELF-vet the project_local tier SUPPRESSES the finding rows — same payload, no rows.
  * Report section + persistence: the `## Structural Quality Metrics` block carries the fixed
    radon-vs-lizard provenance note and NO A–F rank; the payload round-trips onto the run record
    and the vetting_runs schema carries the column.
  * Size cap: a >1MB file is skipped with a coverage disclosure (ran-with-disclosure, not a gap row).

Run directly or via run_smokes.py.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import lizard
from code_vetting_plugin.live_state import get_vetting_runs_schema
from code_vetting_plugin.models import Dimension, Severity
from code_vetting_plugin.report import DEFAULT_ZERO_FP_DIMENSIONS
from code_vetting_plugin.run_record import AllowlistDelta, RunTarget, build_run_metrics
from code_vetting_plugin.runner import SCANNERS, Applicability
from code_vetting_plugin.scanners.structural_metrics import (  # noqa: PLC2701 — pin the internal metric/band logic
    LIZARD_PINNED_VERSION,
    _is_tracked_debt,
    render_structural_metrics_section,
    scan,
    supported_languages,
)
from code_vetting_plugin.targets import TargetTree

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


_MOD_PY = (
    "def clean(a, b):\n"
    "    return a + b\n\n\n"
    "def many_params(a, b, c, d, e, f, g, h, i, j):\n"
    "    return a\n\n\n"
    "def deeply_nested(x):\n"
    "    if x > 0:\n"
    "        for i in range(x):\n"
    "            while i > 0:\n"
    "                if i % 2 == 0:\n"
    "                    for j in range(i):\n"
    "                        if j > 1:\n"
    "                            if j < 5:\n"
    "                                return j\n"
    "    return 0\n"
)
_APP_TS = (
    "export function score(a: number, b: number): number {\n"
    "  if (a > 0 && b > 0) {\n"
    "    return a + b;\n"
    "  }\n"
    "  return 0;\n"
    "}\n"
)


def _big_py() -> str:
    """A function with 35 branches → CCN 36 (crosses the >30 MEDIUM band deterministically)."""
    lines = ["def big_branch(v):", "    total = 0"]
    for n in range(35):
        lines += [f"    if v == {n}:", f"        total += {n}"]
    lines.append("    return total")
    return "\n".join(lines) + "\n"


# EXACT probed metrics (file, name) -> (ccn, nloc, params, nesting). Deterministic under lizard 1.23.0.
_EXPECTED: dict[tuple[str, str], tuple[int, int, int, int]] = {
    ("big.py", "big_branch"): (36, 73, 1, 35),
    ("mod.py", "clean"): (1, 2, 2, 0),
    ("mod.py", "many_params"): (1, 2, 10, 0),
    ("mod.py", "deeply_nested"): (8, 10, 1, 7),
    ("src/app.ts", "score"): (3, 6, 2, 2),
}


def _fixture(root: Path, *, foreign: bool) -> TargetTree:
    (root / "src").mkdir(parents=True)
    (root / "mod.py").write_text(_MOD_PY, encoding="utf-8")
    (root / "big.py").write_text(_big_py(), encoding="utf-8")
    (root / "src" / "app.ts").write_text(_APP_TS, encoding="utf-8")
    if foreign:
        return TargetTree.from_walk(root)
    tracked = ("mod.py", "big.py", "src/app.ts")
    return TargetTree(root=root, tracked=tracked, enumeration="git", foreign=False)


def _check_roster_and_pin() -> None:
    _check("roster is 20 (structural_metrics + python_type_check + rulebook_sync added)", len(SCANNERS) == 20, str(len(SCANNERS)))
    spec = next((s for s in SCANNERS if s.name == "structural_metrics"), None)
    _check(
        "structural_metrics: UNIVERSAL, parse-only (executes_target_code=False)",
        spec is not None and spec.applicability is Applicability.UNIVERSAL and spec.executes_target_code is False,
        str(spec),
    )
    _check("lizard pinned exact to the running version", LIZARD_PINNED_VERSION == "1.23.0" == lizard.version, f"{LIZARD_PINNED_VERSION} / {lizard.version}")


def _check_languages() -> None:
    langs = supported_languages()
    _check("supported languages include ts/tsx/js/python", {"typescript", "tsx", "javascript", "python"} <= set(langs), str(langs))
    _check("dart is NOT supported (flagged Phase-2)", "dart" not in langs, str(langs))


def _check_exact_metrics(tree: TargetTree) -> None:
    report = scan(tree, "vr-r8").structural_metrics
    _check("payload present", report is not None, "")
    assert report is not None
    _check("files_parsed == 3", report.files_parsed == 3, str(report.files_parsed))
    _check("functions_analyzed == 5", report.functions_analyzed == 5, str(report.functions_analyzed))
    _check("languages == (python, typescript)", report.languages == ("python", "typescript"), str(report.languages))
    by_key = {(fn.file, fn.name): fn for fn in report.worst_offenders}
    for key, (ccn, nloc, params, nesting) in _EXPECTED.items():
        fn = by_key.get(key)
        _check(
            f"EXACT metrics for {key[1]} ({key[0]})",
            fn is not None and (fn.ccn, fn.nloc, fn.params, fn.nesting) == (ccn, nloc, params, nesting),
            f"got {None if fn is None else (fn.ccn, fn.nloc, fn.params, fn.nesting)} want {(ccn, nloc, params, nesting)}",
        )
    _check("CCN distribution exact", report.ccn_distribution == {"1-5": 3, "6-10": 1, "11-15": 0, "16-30": 0, ">30": 1}, str(report.ccn_distribution))
    _check("median CCN == 3.0", report.median_ccn == 3.0, str(report.median_ccn))
    _check("p90 CCN == 36.0", report.p90_ccn == 36.0, str(report.p90_ccn))
    _check("worst offender is big_branch", report.worst_offenders[0].name == "big_branch", report.worst_offenders[0].name)


def _check_foreign_findings(tree: TargetTree) -> None:
    findings = scan(tree, "vr-r8").findings
    got = {(f.file, f.constraint_violated, f.severity.value) for f in findings}
    want = {
        ("big.py", "structural:ccn>30", "medium"),
        ("big.py", "structural:nesting>6", "low"),
        ("mod.py", "structural:nesting>6", "low"),
        ("mod.py", "structural:params>8", "low"),
    }
    _check("foreign emits EXACTLY the 4 band findings", got == want, f"got {sorted(got)}")
    _check("severity NEVER exceeds MEDIUM (no HIGH/BLOCKER)", all(f.severity in {Severity.LOW, Severity.MEDIUM} for f in findings), str([f.severity.value for f in findings]))
    _check("all findings are COMPLEXITY dimension", all(f.dimension is Dimension.COMPLEXITY for f in findings), "")
    _check("COMPLEXITY is zero-FP-promoted (renders without an L3 pass)", Dimension.COMPLEXITY in DEFAULT_ZERO_FP_DIMENSIONS, "")
    _check("a clean function (ccn 1) emits no finding", not any(f.evidence.startswith("python function 'clean'") for f in findings), "")


def _check_self_suppression(tree: TargetTree) -> None:
    result = scan(tree, "vr-r8-self")
    _check("self-vet SUPPRESSES metric finding rows (project_local tier)", result.findings == [], str(result.findings))
    _check("self-vet still carries the full payload", result.structural_metrics is not None and result.structural_metrics.functions_analyzed == 5, "")
    _check("coverage always ran=True (lizard structurally present)", result.coverage.ran is True and result.coverage.files_examined == 3, str(result.coverage))


def _check_section_and_persistence(tree: TargetTree) -> None:
    report = scan(tree, "vr-r8").structural_metrics
    section = render_structural_metrics_section(report)
    _check("section header present", section.startswith("## Structural Quality Metrics"), section[:60])
    _check("section carries the radon-vs-lizard provenance note", "numerically comparable" in section and "boolean operators" in section, "")
    _check("section renders the distribution + worst offenders", "CCN band" in section and "Worst offenders" in section and "big_branch" in section, "")
    _check("section renders NO A–F rank vocabulary", not any(f" {letter} " in section for letter in ("A", "B", "C", "D", "E", "F")), "rank letter leaked")
    _check("None payload → stable placeholder", render_structural_metrics_section(None) == "## Structural Quality Metrics\n\n_No structural metrics recorded._", "")
    # Persistence: the payload rides the run record + the schema carries the column.
    assert report is not None
    metrics = build_run_metrics(
        run_id="vr-r8", target=RunTarget(repo="t", ref="r", scope="s"), started="a", finished="b",
        substrate="heuristic", layers_run=[], findings=[], coverage=[], allowlist_delta=AllowlistDelta(totals={}),
        structural_metrics=report.to_dict(),
    )
    row = metrics.to_dict()
    _check("run record carries structural_metrics", isinstance(row.get("structural_metrics"), dict), str(type(row.get("structural_metrics"))))
    _check("persisted payload round-trips the distribution", row["structural_metrics"]["ccn_distribution"][">30"] == 1, str(row["structural_metrics"]))
    _check("vetting_runs schema declares the structural_metrics column", "structural_metrics" in get_vetting_runs_schema().tables["vetting_runs"].columns, "")


def _check_size_cap_and_debt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "big"
        root.mkdir()
        (root / "huge.py").write_text("x = 1\n" + "# pad\n" * 400_000, encoding="utf-8")  # > 1 MB
        (root / "small.py").write_text("def f(a):\n    return a\n", encoding="utf-8")
        result = scan(TargetTree.from_walk(root), "vr-r8-cap")
        report = result.structural_metrics
        assert report is not None
        _check("oversize file skipped (not parsed)", report.files_parsed == 1 and report.files_skipped == 1, str((report.files_parsed, report.files_skipped)))
        _check("skip disclosed on a ran=True coverage row (not a gap)", result.coverage.ran is True and "skipped" in (result.coverage.gap_reason or ""), str(result.coverage))
    # tracked-debt matcher (self-vet annotation): <path-suffix>::<function> convention.
    allow = frozenset({"plugins/x/src/x/mod.py::heavy"})
    _check("tracked-debt matches path-suffix::function", _is_tracked_debt("plugins/x/src/x/mod.py", "heavy", allow), "")
    _check("tracked-debt does not match a different function", not _is_tracked_debt("plugins/x/src/x/mod.py", "light", allow), "")


def _check_literal_frequency() -> None:
    """R9-B: repeated non-trivial literals are surfaced; the trivial/sparse floor drops the rest."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "lit"
        root.mkdir()
        base = 'a = "config_value_here"\nb = 7777\nc = "ab"\nd = 1\ne = 100\n'  # non-trivial x2 + trivial x3
        (root / "f0.py").write_text(base + 'z = "single_file_literal"\n' * 6 + 'p = "two_site_only"\n', encoding="utf-8")
        (root / "f1.py").write_text(base + 'p = "two_site_only"\n', encoding="utf-8")
        (root / "f2.py").write_text(base, encoding="utf-8")
        (root / "f3.py").write_text(base, encoding="utf-8")
        report = scan(TargetTree.from_walk(root), "vr-r9-lit").structural_metrics
        assert report is not None
        values = {(literal.kind, literal.value) for literal in report.literals}
        _check("literal-freq: repeated non-trivial string included (4 sites/4 files)", ("string", "config_value_here") in values, str(values))
        _check("literal-freq: repeated non-trivial number included", ("number", "7777") in values, str(values))
        _check("literal-freq FLOOR: short string (<4 chars) excluded", ("string", "ab") not in values, str(values))
        _check("literal-freq FLOOR: trivial number 1 excluded", ("number", "1") not in values, str(values))
        _check("literal-freq FLOOR: round power of ten (100) excluded", ("number", "100") not in values, str(values))
        _check("literal-freq FLOOR: single-file literal excluded (<2 files)", ("string", "single_file_literal") not in values, str(values))
        _check("literal-freq FLOOR: two-site literal excluded (<4 sites)", ("string", "two_site_only") not in values, str(values))


def main() -> int:
    try:
        _check_roster_and_pin()
        _check_languages()
        _check_literal_frequency()
        with tempfile.TemporaryDirectory() as tmp:
            _check_exact_metrics(_fixture(Path(tmp) / "f1", foreign=True))
        with tempfile.TemporaryDirectory() as tmp:
            _check_foreign_findings(_fixture(Path(tmp) / "f2", foreign=True))
        with tempfile.TemporaryDirectory() as tmp:
            _check_self_suppression(_fixture(Path(tmp) / "f3", foreign=False))
        with tempfile.TemporaryDirectory() as tmp:
            _check_section_and_persistence(_fixture(Path(tmp) / "f4", foreign=True))
        _check_size_cap_and_debt()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"structural_metrics_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
