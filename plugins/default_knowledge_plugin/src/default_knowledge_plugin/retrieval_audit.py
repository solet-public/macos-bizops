"""Executor for ``service_interface::knowledge_service::audit_retrieval_corpus``.

Corpus-wide retrieval audit. Where ``retrieval_test`` evaluates ONE article,
this walker discovers every ``*.retrieval_test.yaml`` companion file in the
corpus, runs the per-article retrieval test against each, and aggregates the
findings into a single report (per ``workbench/
2026-05-26_coordinator_workflow_strategy.md`` §3.2).

Pure executor logic, decoupled from ``DefaultKnowledgePlugin`` so the plugin
method stays a thin wrapper and the aggregation can be smoked with a stubbed
per-article runner. The plugin injects the real ``run_single`` callable, which
delegates to ``DefaultKnowledgePlugin.test_retrieval`` (W2-D2 option (a): a
plain in-process method call, not a queue dispatch).

Finding semantics mirror ``retrieval_test`` exactly, reconstructed from the
structured per-article report rather than by parsing its human-readable
``failures`` strings, where rank is 1-indexed and smaller is better:

- DRIFT: a target query left the article absent or ranked worse than the
  article's ``min_rank``.
- OVERREACH: a forbidden query ranked the article better than its
  ``forbidden_min_rank``.
- STALE_PROCESS_KEY: an asserted process key no longer resolves in the
  live registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .retrieval_test import parse_article_path

TEST_FILE_GLOB = "*.retrieval_test.yaml"
DEFAULT_MIN_RANK = 3
DEFAULT_FORBIDDEN_MIN_RANK = 4

# (case) -> a RetrievalTestReport dict (the ``data`` payload of test_retrieval).
RunSingleFn = Callable[["AuditCase"], dict[str, Any]]


@dataclass(frozen=True)
class AuditCase:
    """One article's retrieval-test parameters parsed from a companion yaml."""

    article_path: str
    target_queries: list[str]
    forbidden_queries: list[str]
    min_rank: int
    forbidden_min_rank: int
    process_key_assertions: list[str]
    source_file: str

    @property
    def knowledge_base(self) -> str:
        """Knowledge-base name the article belongs to (``article_path`` part 1)."""
        return parse_article_path(self.article_path).knowledge_base


@dataclass(frozen=True)
class AuditAggregate:
    """Aggregated outcome across every audited article."""

    total_articles_audited: int
    passed: int
    failed: int
    drifts: list[dict[str, Any]] = field(default_factory=list)
    overreaches: list[dict[str, Any]] = field(default_factory=list)
    stale_keys: list[dict[str, Any]] = field(default_factory=list)
    passing_articles: list[str] = field(default_factory=list)


def discover_test_files(corpus_root: Path) -> list[Path]:
    """Return every ``*.retrieval_test.yaml`` under ``corpus_root``, path-sorted."""
    return sorted(corpus_root.rglob(TEST_FILE_GLOB))


def _as_str_list(value: Any) -> list[str]:
    """Coerce a yaml scalar/sequence into a list of strings; [] when absent."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def parse_test_file(path: Path) -> AuditCase:
    """Parse one ``*.retrieval_test.yaml`` into an ``AuditCase``.

    Raises ``ValueError`` when the file is not a mapping or omits the
    required ``article_path`` field.
    """
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Retrieval-test file is not a mapping: {path}")
    article_path = loaded.get("article_path")
    if not isinstance(article_path, str) or not article_path.strip():
        raise ValueError(f"Retrieval-test file missing 'article_path': {path}")
    return AuditCase(
        article_path=article_path.strip(),
        target_queries=_as_str_list(loaded.get("target_queries")),
        forbidden_queries=_as_str_list(loaded.get("forbidden_queries")),
        min_rank=int(loaded.get("min_rank", DEFAULT_MIN_RANK)),
        forbidden_min_rank=int(
            loaded.get("forbidden_min_rank", DEFAULT_FORBIDDEN_MIN_RANK)
        ),
        process_key_assertions=_as_str_list(loaded.get("process_key_assertions")),
        source_file=str(path),
    )


def filter_cases_to_active(
    cases: list[AuditCase],
    active_names: list[str],
    requested: list[str] | None,
) -> list[AuditCase]:
    """Restrict cases to active knowledge bases.

    ``requested`` None audits every active KB's articles. A supplied subset
    must be a subset of the active set; otherwise ``ValueError`` is raised,
    mirroring ``retrieval_test.build_search_fn``.
    """
    active_set = set(active_names)
    if requested is None:
        allowed = active_set
    else:
        missing = sorted(kb for kb in requested if kb not in active_set)
        if missing:
            raise ValueError(f"Requested knowledge bases are not active: {missing}")
        allowed = set(requested)
    return [case for case in cases if case.knowledge_base in allowed]


def _classify_drifts(
    case: AuditCase, target_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct DRIFT findings from a case's target-query observations."""
    drifts: list[dict[str, Any]] = []
    for observation in target_results:
        rank = observation.get("observed_rank")
        if rank is None or int(rank) > case.min_rank:
            drifts.append({
                "article_path": case.article_path,
                "query": observation.get("query"),
                "observed_rank": rank,
                "min_rank": case.min_rank,
            })
    return drifts


def _classify_overreaches(
    case: AuditCase, forbidden_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct OVERREACH findings from a case's forbidden-query observations."""
    overreaches: list[dict[str, Any]] = []
    for observation in forbidden_results:
        rank = observation.get("observed_rank")
        if rank is not None and int(rank) < case.forbidden_min_rank:
            overreaches.append({
                "article_path": case.article_path,
                "query": observation.get("query"),
                "observed_rank": rank,
                "forbidden_min_rank": case.forbidden_min_rank,
            })
    return overreaches


def _classify_stale(
    case: AuditCase, freshness: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct STALE_PROCESS_KEY findings from a case's freshness records."""
    return [
        {"article_path": case.article_path, "process_key": record.get("referenced_key")}
        for record in freshness
        if not record.get("exists_in_registry", False)
    ]


def aggregate_audit(
    *, cases: list[AuditCase], run_single: RunSingleFn, fail_fast: bool,
) -> AuditAggregate:
    """Run each case through ``run_single`` and aggregate the findings.

    With ``fail_fast`` the walk stops after the first article that produces
    any finding; that article's findings are still included.
    """
    drifts: list[dict[str, Any]] = []
    overreaches: list[dict[str, Any]] = []
    stale_keys: list[dict[str, Any]] = []
    passing_articles: list[str] = []
    passed = 0
    failed = 0
    for case in cases:
        report = run_single(case)
        case_drifts = _classify_drifts(case, report.get("target_results", []))
        case_over = _classify_overreaches(case, report.get("forbidden_results", []))
        case_stale = _classify_stale(case, report.get("process_key_freshness", []))
        if case_drifts or case_over or case_stale:
            failed += 1
            drifts.extend(case_drifts)
            overreaches.extend(case_over)
            stale_keys.extend(case_stale)
            if fail_fast:
                break
        else:
            passed += 1
            passing_articles.append(case.article_path)
    return AuditAggregate(
        total_articles_audited=passed + failed,
        passed=passed,
        failed=failed,
        drifts=drifts,
        overreaches=overreaches,
        stale_keys=stale_keys,
        passing_articles=passing_articles,
    )


def _render_drift_lines(drifts: list[dict[str, Any]]) -> list[str]:
    """Markdown lines for the DRIFT section."""
    lines = ["## DRIFT", ""]
    if not drifts:
        lines.append("None.")
        return lines
    for drift in drifts:
        lines.append(
            f"- `{drift['article_path']}` — query {drift['query']!r} "
            f"ranked {drift['observed_rank']} (expected <= {drift['min_rank']})"
        )
    return lines


def _render_overreach_lines(overreaches: list[dict[str, Any]]) -> list[str]:
    """Markdown lines for the OVERREACH section."""
    lines = ["## OVERREACH", ""]
    if not overreaches:
        lines.append("None.")
        return lines
    for over in overreaches:
        lines.append(
            f"- `{over['article_path']}` — forbidden query {over['query']!r} "
            f"ranked {over['observed_rank']} (expected absent or "
            f">= {over['forbidden_min_rank']})"
        )
    return lines


def _render_stale_lines(stale_keys: list[dict[str, Any]]) -> list[str]:
    """Markdown lines for the STALE_PROCESS_KEY section."""
    lines = ["## STALE_PROCESS_KEY", ""]
    if not stale_keys:
        lines.append("None.")
        return lines
    for stale in stale_keys:
        lines.append(
            f"- `{stale['article_path']}` — `{stale['process_key']}` "
            "not found in process registry"
        )
    return lines


def _render_passing_lines(passing_articles: list[str]) -> list[str]:
    """Markdown lines for the PASSING articles section."""
    lines = ["## PASSING articles", ""]
    if not passing_articles:
        lines.append("None.")
        return lines
    lines.extend(f"- `{article}`" for article in passing_articles)
    return lines


def render_markdown(
    *,
    aggregate: AuditAggregate,
    ran_at: str,
    corpus_root: str,
    report_path: str,
    duration_seconds: float,
) -> str:
    """Render the aggregate as a Markdown audit report."""
    header = [
        "# Knowledge Base Retrieval Audit",
        "",
        f"- Ran at: {ran_at}",
        f"- Corpus root: `{corpus_root}`",
        f"- Articles audited: {aggregate.total_articles_audited}",
        f"- Passed: {aggregate.passed}",
        f"- Failed: {aggregate.failed}",
        f"- Duration: {duration_seconds:.3f}s",
        f"- Report path: `{report_path}`",
        "",
    ]
    sections = [
        _render_drift_lines(aggregate.drifts),
        _render_overreach_lines(aggregate.overreaches),
        _render_stale_lines(aggregate.stale_keys),
        _render_passing_lines(aggregate.passing_articles),
    ]
    lines = list(header)
    for section in sections:
        lines.extend(section)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
