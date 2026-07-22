"""structural_metrics.py — R8-1: the multi-language structural-quality metrics scanner.

ONE `UNIVERSAL`, parse-only, ungated L1 scanner (`executes_target_code=False`) that gives
FOREIGN targets the deterministic best-practices analysis they get zero of today (complexity
is the platform-self+Python via the radon/god-class gate wrappers). Backed by **lizard 1.23.0** as a
PINNED in-process library dependency (Architect ruling §B, probed 2026-07-21): TS/TSX/JS/Python
+ 23 more languages, per-function CCN/NLOC/params/nesting via tokenization — it READS target
text and never executes it, so it is structurally distinct from the R7-4 tsc/eslint tier.

v1 (this slice) = function-level metrics + per-file aggregates + persisted distributions.
Class-level god-class metrics are v2 (tree-sitter); the maintainability-index composite is
REJECTED permanently as an export (we reformed it internally because it misleads) — the report
carries MI's ingredients (CCN/NLOC/distributions), never the composite (ruling §A).

Posture is **report-not-gate everywhere** (ruling §C/§D): the commit gates stay the sole gate
authority. Band-crossing findings (COMPLEXITY dimension, ceiling MEDIUM, Confirmed) are emitted
only on FOREIGN runs (the universal tier); on a self-vet the project_local tier suppresses the
metric Finding rows — radon/god-class already own that surface, and double-reporting the same
fact through two vocabularies is noise. Both personas get the report SECTION. Vocabulary is kept
separate by construction: lizard CCN (counts boolean operators) is never rendered in radon's A–F
rank vocabulary and never converted — the two numbers are not comparable and the report says so.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import lizard
import lizard_languages

from ..coverage import CoverageRecord, ScannerResult
from ..models import ContextProfile, Dimension, Finding, Layer, Provenance, Severity
from ..targets import TargetTree

# Pinned exact (ruling §B): a lizard upgrade that changes CCN counting would silently corrupt
# every trend line, so the version is asserted at scan time and recorded in the report/provenance.
LIZARD_PINNED_VERSION = "1.23.0"

_SCANNER = "structural_metrics"
# Per-file byte cap: in-process parsing has no subprocess timeout, so a pathological file is
# skipped with a coverage note rather than hanging the scan (ruling §B guard).
_MAX_FILE_BYTES = 1_000_000
# Bound the persisted/rendered detail so the vetting_runs row + report stay bounded (F1 §3):
# distributions + aggregates are the trend signal; the per-function detail is the worst-N only.
_WORST_OFFENDERS_CAP = 100
_MAX_FINDINGS = 200

# R9-B literal-frequency table (evidence-only — structurally NO Finding path). Trivial floor is the
# ONLY filtering (everything above it is L2's judgment call): short strings, {-1,0,1,2}+round powers
# of ten, and literals too sparse to be a config/enum candidate are dropped.
_LITERAL_CAP = 30
_MIN_LITERAL_SITES = 4
_MIN_LITERAL_FILES = 2
_MIN_STRING_LEN = 4
_TRIVIAL_NUMBERS: frozenset[float] = frozenset({0.0, 1.0, 2.0})
_NUMBER_TOKEN = re.compile(r"^\d[\d_]*(?:\.\d+)?$")  # tokens are unsigned — a leading '-' is its own token


class Metric(StrEnum):
    """The four universal, industry-anchored per-function metrics (ruling §A)."""

    CCN = "ccn"
    NLOC = "nloc"
    PARAMS = "params"
    NESTING = "nesting"


# Universal-tier bands (ruling §C): (attention, LOW-finding, MEDIUM-finding). MEDIUM=None means the
# metric's finding ceiling is LOW (a structural metric can never be "exploitable now" → never HIGH).
# Anchors: McCabe 10 / lizard default 15 / NIST 500-235 "15 with rigor". lizard CCN counts boolean
# operators (extended-CCN), so the bands sit slightly above radon-vocabulary equivalents.
_BANDS: dict[Metric, tuple[int, int, int | None]] = {
    Metric.CCN: (10, 15, 30),
    Metric.NLOC: (60, 100, 200),
    Metric.PARAMS: (5, 8, None),
    Metric.NESTING: (4, 6, None),
}
# CCN distribution buckets rendered in the section (ruling §C.2). Upper-exclusive ranges + a tail.
_CCN_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1-5", 1, 6),
    ("6-10", 6, 11),
    ("11-15", 11, 16),
    ("16-30", 16, 31),
    (">30", 31, None),
)


@dataclass(frozen=True, slots=True)
class FunctionMetric:
    """One function's structural metrics — a deterministic, zero-FP fact."""

    file: str
    line: int
    name: str
    language: str
    ccn: int
    nloc: int
    params: int
    nesting: int
    tracked_debt: bool = False  # self-vet only: this function is in radon_cc_allowlist (visible debt)

    def value(self, metric: Metric) -> int:
        return {Metric.CCN: self.ccn, Metric.NLOC: self.nloc, Metric.PARAMS: self.params, Metric.NESTING: self.nesting}[metric]

    def to_dict(self) -> dict[str, object]:
        return {
            "file": self.file, "line": self.line, "name": self.name, "language": self.language,
            "ccn": self.ccn, "nloc": self.nloc, "params": self.params, "nesting": self.nesting,
            "tracked_debt": self.tracked_debt,
        }


@dataclass(frozen=True, slots=True)
class FileAggregate:
    """Per-file structural aggregate — the v1 stand-in for structural-hotspot detection (ruling §A)."""

    file: str
    language: str
    nloc: int
    function_count: int
    max_ccn: int

    def to_dict(self) -> dict[str, object]:
        return {"file": self.file, "language": self.language, "nloc": self.nloc, "function_count": self.function_count, "max_ccn": self.max_ccn}


@dataclass(frozen=True, slots=True)
class LiteralFrequency:
    """One repeated non-trivial literal (R9-B) — evidence for the L2 magic-strings lens, never a
    finding. ``value`` is the normalized literal (string inner text / numeric token)."""

    kind: str  # "string" | "number"
    value: str
    occurrences: int
    files: int

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "value": self.value, "occurrences": self.occurrences, "files": self.files}


@dataclass(slots=True)
class _LitAccum:
    """Mutable per-literal accumulator (occurrence count + distinct-file set)."""

    occurrences: int = 0
    files: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class StructuralMetricsReport:
    """The bounded, persisted/rendered structural-metrics payload for one run (ruling §E).

    Distributions + aggregates + summary are the trend signal (fully carried); the per-function
    detail is capped to the worst-N by CCN so the vetting_runs row cannot become its own leak.
    """

    tool: str
    tool_version: str
    files_parsed: int
    files_skipped: int
    functions_analyzed: int
    languages: tuple[str, ...]
    median_ccn: float
    p90_ccn: float
    ccn_distribution: dict[str, int]
    worst_offenders: tuple[FunctionMetric, ...]
    file_aggregates: tuple[FileAggregate, ...]
    literals: tuple[LiteralFrequency, ...]  # R9-B: top repeated non-trivial literals (evidence-only)

    def to_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "tool_version": self.tool_version,
            "files_parsed": self.files_parsed,
            "files_skipped": self.files_skipped,
            "functions_analyzed": self.functions_analyzed,
            "languages": list(self.languages),
            "median_ccn": self.median_ccn,
            "p90_ccn": self.p90_ccn,
            "ccn_distribution": dict(self.ccn_distribution),
            "worst_offenders": [fn.to_dict() for fn in self.worst_offenders],
            "file_aggregates": [agg.to_dict() for agg in self.file_aggregates[:_WORST_OFFENDERS_CAP]],
            "literals": [literal.to_dict() for literal in self.literals],
        }


def supported_languages() -> tuple[str, ...]:
    """The sorted set of languages lizard can analyze (its readers' primary names).

    Recorded so a smoke pins it — a lizard upgrade that silently changes the supported set (and
    thus foreign coverage) turns the language-set assertion red instead of drifting unnoticed.
    """
    names: set[str] = set()
    for reader in lizard_languages.languages():
        names.update(reader.language_names)
    return tuple(sorted(names))


def _analyzer() -> lizard.FileAnalyzer:
    """A FRESH lizard file analyzer with the nesting-DEPTH (`nd`) extension (ruling §B).

    Built per-file ON PURPOSE: lizard's nesting extension accumulates its counter across analyze
    calls on a shared analyzer instance, which contaminates ``max_nesting_depth`` for every file
    after the first. A fresh instance per file keeps the per-function depth correct (probed).
    """
    return lizard.FileAnalyzer(lizard.get_extensions(["nd"]))


def _language_for(rel: str) -> str | None:
    """The lizard language label for a path, or None if lizard cannot analyze it (no drift —
    lizard's own reader table is the single source of truth for 'supported')."""
    reader = lizard_languages.get_reader_for(rel)
    return reader.language_names[0] if reader is not None else None


def _load_tracked_debt(tree: TargetTree) -> frozenset[str]:
    """The self-vet radon_cc_allowlist entries (``<path>::<function>``) for the tracked-debt
    annotation. Foreign targets have no such register (the file is absent) → empty (ruling §C)."""
    allowlist = tree.root / "quality_gates" / "radon_cc_allowlist.txt"
    if tree.foreign or not allowlist.is_file():
        return frozenset()
    entries: set[str] = set()
    for raw in allowlist.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return frozenset(entries)


def _is_tracked_debt(file: str, name: str, allowlist: frozenset[str]) -> bool:
    """Match the gate's ``<path-suffix>::<function>`` convention (approximate — annotation only)."""
    for entry in allowlist:
        path_part, _, fn_part = entry.partition("::")
        if fn_part == name and (file == path_part or file.endswith(path_part)):
            return True
    return False


def _analyze_file(tree: TargetTree, rel: str, language: str, allowlist: frozenset[str]) -> list[FunctionMetric]:
    """Parse one file into per-function metrics (nesting depth via the ``nd`` extension's
    ``max_nesting_depth``; a fresh analyzer per file avoids the extension's cross-file drift)."""
    info: Any = _analyzer()(str(tree.abspath(rel)))
    metrics: list[FunctionMetric] = []
    for fn in info.function_list:
        metrics.append(
            FunctionMetric(
                file=rel,
                line=int(fn.start_line),
                name=str(fn.name),
                language=language,
                ccn=int(fn.cyclomatic_complexity),
                nloc=int(fn.nloc),
                params=int(fn.parameter_count),
                nesting=int(getattr(fn, "max_nesting_depth", 0)),
                tracked_debt=_is_tracked_debt(rel, str(fn.name), allowlist),
            )
        )
    return metrics


def _is_power_of_ten(value: int) -> bool:
    if value < 10:
        return False
    while value % 10 == 0 and value > 1:
        value //= 10
    return value == 1


def _literal_of(token: str) -> tuple[str, str] | None:
    """(kind, normalized-value) for a NON-trivial string/number literal token, else None (R9-B).

    Trivial floor is the only filtering: strings shorter than 4 chars, the numbers {-1,0,1,2} (a
    leading '-' is a separate token, so '1' covers -1), and round powers of ten."""
    if not token:
        return None
    if token[0] in "\"'`":
        value = token[1:-1] if len(token) >= 2 else ""
        return ("string", value) if len(value) >= _MIN_STRING_LEN else None
    if _NUMBER_TOKEN.match(token):
        number = float(token.replace("_", ""))
        if number in _TRIVIAL_NUMBERS or (number.is_integer() and _is_power_of_ten(int(number))):
            return None
        return ("number", token)
    return None


def _accumulate_literals(counts: dict[tuple[str, str], _LitAccum], rel: str, code: str) -> None:
    """Tokenize one file (lizard's per-language tokenizer) and tally its non-trivial literals."""
    reader = lizard_languages.get_reader_for(rel)
    if reader is None:
        return
    for token in reader.generate_tokens(code):
        literal = _literal_of(token)
        if literal is None:
            continue
        accum = counts.setdefault(literal, _LitAccum())
        accum.occurrences += 1
        accum.files.add(rel)


def _finalize_literals(counts: dict[tuple[str, str], _LitAccum]) -> tuple[LiteralFrequency, ...]:
    """Apply the sparsity floor (≥4 sites AND ≥2 files) + cap to the top repeated literals."""
    floored = [
        LiteralFrequency(kind=kind, value=value, occurrences=accum.occurrences, files=len(accum.files))
        for (kind, value), accum in counts.items()
        if accum.occurrences >= _MIN_LITERAL_SITES and len(accum.files) >= _MIN_LITERAL_FILES
    ]
    floored.sort(key=lambda literal: (-literal.occurrences, -literal.files, literal.value))
    return tuple(floored[:_LITERAL_CAP])


def _collect(tree: TargetTree) -> tuple[list[FunctionMetric], int, int, list[str], tuple[LiteralFrequency, ...]]:
    """Walk the enumerated tree ∩ lizard-supported suffixes; parse each (size-capped). Returns the
    function metrics, files-parsed, files-skipped (oversize/unreadable), the language set, and the
    repeated-literal table (R9-B — tokenized from the same file set)."""
    allowlist = _load_tracked_debt(tree)
    literal_counts: dict[tuple[str, str], _LitAccum] = {}
    functions: list[FunctionMetric] = []
    parsed = 0
    skipped = 0
    languages: set[str] = set()
    for rel in tree.all_files():
        language = _language_for(rel)
        if language is None:
            continue
        path = tree.abspath(rel)
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            skipped += 1
            continue
        functions.extend(_analyze_file(tree, rel, language, allowlist))
        _accumulate_literals(literal_counts, rel, path.read_text(encoding="utf-8", errors="replace"))
        parsed += 1
        languages.add(language)
    return functions, parsed, skipped, sorted(languages), _finalize_literals(literal_counts)


def _bucket_ccn(functions: list[FunctionMetric]) -> dict[str, int]:
    distribution = {label: 0 for label, _, _ in _CCN_BUCKETS}
    for fn in functions:
        for label, low, high in _CCN_BUCKETS:
            if fn.ccn >= low and (high is None or fn.ccn < high):
                distribution[label] += 1
                break
    return distribution


def _file_aggregates(functions: list[FunctionMetric]) -> list[FileAggregate]:
    by_file: dict[str, list[FunctionMetric]] = {}
    for fn in functions:
        by_file.setdefault(fn.file, []).append(fn)
    aggregates = [
        FileAggregate(
            file=file,
            language=fns[0].language,
            nloc=sum(fn.nloc for fn in fns),
            function_count=len(fns),
            max_ccn=max(fn.ccn for fn in fns),
        )
        for file, fns in by_file.items()
    ]
    aggregates.sort(key=lambda agg: (-agg.max_ccn, -agg.nloc, agg.file))
    return aggregates


def _worst_offenders(functions: list[FunctionMetric]) -> tuple[FunctionMetric, ...]:
    ordered = sorted(functions, key=lambda fn: (-fn.ccn, -fn.nloc, fn.file, fn.line))
    return tuple(ordered[:_WORST_OFFENDERS_CAP])


def _percentile(values: list[int], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(fraction * (len(ordered) - 1) + 0.5))
    return float(ordered[index])


def _build_report(
    functions: list[FunctionMetric], parsed: int, skipped: int, languages: list[str], literals: tuple[LiteralFrequency, ...]
) -> StructuralMetricsReport:
    ccns = [fn.ccn for fn in functions]
    return StructuralMetricsReport(
        tool="lizard",
        tool_version=LIZARD_PINNED_VERSION,
        files_parsed=parsed,
        files_skipped=skipped,
        functions_analyzed=len(functions),
        languages=tuple(languages),
        median_ccn=float(statistics.median(ccns)) if ccns else 0.0,
        p90_ccn=_percentile(ccns, 0.9),
        ccn_distribution=_bucket_ccn(functions),
        worst_offenders=_worst_offenders(functions),
        file_aggregates=tuple(_file_aggregates(functions)),
        literals=literals,
    )


def _band_finding(fn: FunctionMetric, metric: Metric, run_id: str, context: ContextProfile) -> Finding | None:
    """The single highest band a function crosses for one metric → a COMPLEXITY finding, or None.

    Ceiling MEDIUM (a structural metric is never exploitable-now); Confirmed (deterministic
    zero-FP fact); COMPLEXITY dimension (never ARCHITECTURE — that is the the platform-conformance critic's,
    and mixing size facts in would muddy its survival stats — the exact D1 mistake, ruling §C).
    """
    attention, low, medium = _BANDS[metric]
    del attention
    value = fn.value(metric)
    if medium is not None and value > medium:
        severity, threshold = Severity.MEDIUM, medium
    elif value > low:
        severity, threshold = Severity.LOW, low
    else:
        return None
    return Finding.build(
        run_id=run_id,
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.COMPLEXITY,
        severity=severity,
        file=fn.file,
        line=fn.line,
        constraint_violated=f"structural:{metric.value}>{threshold}",
        evidence=f"{fn.language} function '{fn.name}' has {metric.value} {value} (> {threshold}) at {fn.file}:{fn.line}",
        fix_suggestion=None,
        provenance=Provenance(source="lizard", tool_version=LIZARD_PINNED_VERSION, rule_id=metric.value),
        context_profile=context,
    )


def _band_findings(functions: list[FunctionMetric], run_id: str, context: ContextProfile) -> tuple[list[Finding], int]:
    """Every band-crossing → a finding (worst-CCN first, capped); returns findings + total emitted."""
    ordered = sorted(functions, key=lambda fn: (-fn.ccn, fn.file, fn.line))
    findings: list[Finding] = []
    for fn in ordered:
        for metric in Metric:
            finding = _band_finding(fn, metric, run_id, context)
            if finding is not None:
                findings.append(finding)
    total = len(findings)
    return findings[:_MAX_FINDINGS], total


def scan(tree: TargetTree, run_id: str) -> ScannerResult:
    """Parse the tree's lizard-supported files into structural metrics (parse-only, ungated).

    Coverage is ALWAYS ``examined`` (lizard is structurally present — a pinned library, never a
    tool-absent row). Band-crossing findings are emitted ONLY on a FOREIGN target (the universal
    tier); a self-vet keeps the PROJECT_LOCAL tier, which suppresses the metric Finding rows so R8
    never double-reports what the radon/god-class gate already owns (ruling §C). Both personas
    carry the full metrics payload for the report section + trend persistence.
    """
    if lizard.version != LIZARD_PINNED_VERSION:
        raise RuntimeError(
            f"{_SCANNER}: lizard {lizard.version} != pinned {LIZARD_PINNED_VERSION} — a version drift "
            "would silently corrupt CCN trend lines; re-pin deliberately with a re-baseline note."
        )
    functions, parsed, skipped, languages, literals = _collect(tree)
    report = _build_report(functions, parsed, skipped, languages, literals)

    # FT-2 tier-controlled emission (ruling §C), derived from target class. This mirrors
    # ``verify.tiers.active_tiers(foreign=...)`` — a FOREIGN target is UNIVERSAL-tier-only, so the
    # metric findings emit; a self-vet keeps the PROJECT_LOCAL tier, which suppresses them (radon /
    # god-class own that surface). The derivation is inlined rather than importing ``verify.tiers``
    # ON PURPOSE: the R6 verb-import-closure invariant (verb_import_closure_smoke) forbids the
    # L1-only verbs' import closure — which this scanner is in — from transitively importing
    # ``verify/*``. `active_tiers(foreign=True)` == {UNIVERSAL} (no PROJECT_LOCAL → emit); self == both.
    emit = tree.foreign
    findings: list[Finding] = []
    disclosure: str | None = None
    if emit:
        findings, total = _band_findings(functions, run_id, ContextProfile.PRODUCTION)
        if total > _MAX_FINDINGS:
            disclosure = f"emitted {total} band-crossing metrics; first {_MAX_FINDINGS} rendered as findings"
    if skipped and disclosure is None:
        disclosure = f"{skipped} file(s) skipped (>{_MAX_FILE_BYTES // 1000}KB or unreadable)"

    coverage = CoverageRecord(scanner=_SCANNER, ran=True, files_examined=parsed, gap_reason=disclosure)
    return ScannerResult(findings=findings, coverage=coverage, structural_metrics=report)


# --- report section (mirrors test_coverage.render_test_coverage_section; owned here, not in report.py) ---

_PROVENANCE_NOTE = (
    "_lizard CCN counts boolean operators (extended cyclomatic complexity); commit-gate verdicts "
    "come from radon and god-class and are NOT numerically comparable to these figures._"
)


def _distribution_table(report: StructuralMetricsReport) -> list[str]:
    lines = ["| CCN band | functions |", "| --- | --- |"]
    lines.extend(f"| {label} | {report.ccn_distribution.get(label, 0)} |" for label, _, _ in _CCN_BUCKETS)
    return lines


def _worst_offenders_table(report: StructuralMetricsReport) -> list[str]:
    lines = [
        "**Worst offenders** (by cyclomatic complexity)",
        "",
        "| function | location | CCN | NLOC | params | nesting | |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for fn in report.worst_offenders[:10]:
        debt = "tracked debt (allowlisted)" if fn.tracked_debt else ""
        lines.append(f"| `{fn.name}` | `{fn.file}:{fn.line}` | {fn.ccn} | {fn.nloc} | {fn.params} | {fn.nesting} | {debt} |")
    return lines


def _repeated_literals_table(report: StructuralMetricsReport) -> list[str]:
    """R9-B subsection: the top repeated non-trivial literals — evidence for the L2 magic-strings
    lens (config/enum/constant candidates vs i18n prose vs test fixtures), NOT findings."""
    lines = [
        "**Repeated literals** (magic-string/value candidates — evidence for the AI-critic layer, not findings)",
        "",
        "| literal | kind | occurrences | files |",
        "| --- | --- | --- | --- |",
    ]
    for literal in report.literals:
        shown = literal.value if len(literal.value) <= 40 else f"{literal.value[:37]}…"
        lines.append(f"| `{shown}` | {literal.kind} | {literal.occurrences} | {literal.files} |")
    return lines


def _headline(report: StructuralMetricsReport) -> str:
    over_10 = sum(count for label, count in report.ccn_distribution.items() if label in {"11-15", "16-30", ">30"})
    over_15 = sum(count for label, count in report.ccn_distribution.items() if label in {"16-30", ">30"})
    return (
        f"**{report.functions_analyzed} function(s)** across {report.files_parsed} file(s) "
        f"({', '.join(report.languages)}) · median CCN {report.median_ccn:g} · p90 CCN {report.p90_ccn:g} · "
        f"{over_10} over 10 · {over_15} over 15 · lizard v{report.tool_version}"
    )


def _empty_section(report: StructuralMetricsReport) -> str:
    """The no-functions-analyzed block; still renders the literal table when literals were found."""
    langs = ", ".join(report.languages) or "none"
    head = (
        "## Structural Quality Metrics\n\n"
        f"_No functions analyzed — {report.files_parsed} file(s) parsed across {langs}; "
        f"{report.files_skipped} skipped. lizard v{report.tool_version}._"
    )
    return f"{head}\n\n" + "\n".join(_repeated_literals_table(report)) if report.literals else head


def render_structural_metrics_section(report: StructuralMetricsReport | None) -> str:
    """The ``## Structural Quality Metrics`` block (ruling §C: after the Coverage matrix, before
    Findings by Severity in the report-format spec §1 order). Distribution + worst-offenders + the
    R9-B repeated-literals subsection + the fixed radon-vs-lizard note; never an A–F rank / MI."""
    if report is None:
        return "## Structural Quality Metrics\n\n_No structural metrics recorded._"
    if report.functions_analyzed == 0:
        return _empty_section(report)
    blocks = [
        "## Structural Quality Metrics",
        "",
        _headline(report),
        "",
        _PROVENANCE_NOTE,
        "",
        "\n".join(_distribution_table(report)),
        "",
        "\n".join(_worst_offenders_table(report)),
    ]
    if report.literals:
        blocks.extend(("", "\n".join(_repeated_literals_table(report))))
    if report.files_skipped:
        blocks.append(f"\n_{report.files_skipped} file(s) skipped (oversize / unreadable)._")
    return "\n".join(blocks)
