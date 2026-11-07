#!/usr/bin/env python3
"""A4 T1/T2/T3: offline registration consumer, source isolation and harness custody.

No network, database or live model-list command. Registrations use A2's typed
snapshot, readers use injected fixtures, and local caches live under temp HOME.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_messaging_plugin.model_capability_harness import (  # noqa: E402
    JOIN_ALIASES,
    RUNTIME_ADAPTERS,
    HarnessContext,
    HarnessList,
    ProcessResult,
)
from agent_messaging_plugin.model_catalog_policy import CatalogRevision, CatalogSource  # noqa: E402
from agent_messaging_plugin.model_metadata_adapters import (  # noqa: E402
    HARNESS_IMPLEMENTATIONS,
    Discovery,
    MetadataContext,
    Metrics,
    ModelMetric,
    OfferedModel,
    SourceError,
    discover,
    fetch_metrics,
)

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def registration(**changes: object) -> CatalogSource:
    source = CatalogSource(
        CatalogRevision("third", "revision-1", 1, None, None, "config-owner", "fixture", "set", NOW.isoformat(), "active"),
        "third-provider",
        "third-source",
        "fixture-reader",
        "v3",
        "https://third.invalid/models",
        "credential:third",
        {"benchmark": "points", "input_price": "USD/million_tokens", "subscription": "USD/month"},
        None,
        {"Provider Name": "third-model"},
        True,
    )
    # Test-only convenience; production consumes A2's validated typed readback.
    from typing import Any, cast

    return replace(source, **cast(dict[str, Any], changes))


class ThirdProvider:
    """A new installed implementation; no production selector/provider edits."""

    def discover(self, context: MetadataContext, registration: CatalogSource) -> Discovery:
        if registration.endpoint != "https://third.invalid/models":
            raise SourceError(f"parser_unavailable: {registration.endpoint}")
        if registration.credential_ref != "credential:third":
            raise OSError("credential reference unavailable")
        return Discovery((OfferedModel("Provider Name", ("brief", "deep"), 123456),))

    def fetch_metrics(self, context: MetadataContext, registration: CatalogSource, offered_models: Sequence[OfferedModel]) -> Metrics:
        return Metrics(
            (
                ModelMetric("Provider Name", "deep", "benchmark", "points", 81.5),
                ModelMetric("Provider Name", None, "input_price", "USD/million_tokens", 2.5),
                ModelMetric("Provider Name", None, "subscription", "USD/month", 29.0),
            )
        )


class FixedReader:
    def __init__(self, discovery: Discovery, metrics: Metrics) -> None:
        self.discovery = discovery
        self.metrics = metrics

    def discover(self, context: MetadataContext, registration: CatalogSource) -> Discovery:
        return self.discovery

    def fetch_metrics(self, context: MetadataContext, registration: CatalogSource, offered_models: Sequence[OfferedModel]) -> Metrics:
        return self.metrics


def unused_runner(argv: Sequence[str], timeout: float) -> ProcessResult:
    raise AssertionError(f"unexpected process: {argv}")


class MetadataAdaptersSmoke(unittest.TestCase):
    def context(self, home: Path) -> MetadataContext:
        # Individual command tests construct their typed runner context below.
        return MetadataContext(HarnessContext(home, Path(sys.executable), unused_runner, NOW))

    def test_t1_registered_third_provider(self) -> None:
        source = registration()
        implementations = {("fixture-reader", "v3"): ThirdProvider()}
        context = self.context(Path("/unused"))
        outcome = discover(context, source, implementations)
        self.assertEqual(outcome.status, "ok")
        self.assertIs(outcome.registration, source)
        self.assertIsNotNone(outcome.observed)
        assert outcome.observed is not None
        self.assertIsNone(outcome.observed.launchability)
        self.assertEqual(outcome.observed.models, (OfferedModel("third-model", ("brief", "deep"), 123456),))
        metrics = fetch_metrics(context, source, outcome.observed.models, implementations)
        self.assertEqual(metrics.status, "ok")
        assert metrics.observed is not None
        self.assertEqual(
            [(item.metric, item.unit, item.value) for item in metrics.observed.observations],
            [
                ("benchmark", "points", 81.5),
                ("input_price", "USD/million_tokens", 2.5),
                ("subscription", "USD/month", 29.0),
            ],
        )
        self.assertEqual({item.model_id for item in metrics.observed.observations}, {"third-model"})
        corrected = replace(source, aliases={"Provider Name": "corrected-model"}, provenance=replace(source.provenance, revision=2, revision_id="revision-2"))
        changed = discover(context, corrected, implementations, prior=outcome.observed)
        assert changed.observed is not None
        self.assertEqual(changed.observed.models[0].model_id, "corrected-model")
        self.assertIs(changed.prior, outcome.observed)
        self.assertEqual(set(RUNTIME_ADAPTERS), {"codex", "claude_code"})

    def test_t2_failure_retention_and_independent_success(self) -> None:
        context = self.context(Path("/unused"))
        source = registration()
        implementations = {("fixture-reader", "v3"): ThirdProvider()}
        prior = Discovery((OfferedModel("third-model", ("deep",), None),))
        prior_metrics = Metrics((ModelMetric("third-model", "deep", "benchmark", "points", 70.0),))
        for changed, detail in (
            (replace(source, implementation_version="missing"), "adapter_unavailable"),
            (replace(source, endpoint="https://third.invalid/unsupported"), "parser_unavailable"),
            (replace(source, credential_ref=None), "credential reference unavailable"),
        ):
            outcome = discover(context, changed, implementations, prior=prior)
            self.assertEqual(outcome.status, "failed")
            self.assertIn(detail, outcome.failure or "")
            self.assertIs(outcome.best_available, prior)
            self.assertTrue(outcome.advisories)
        unavailable = fetch_metrics(context, replace(source, implementation_version="missing"), prior.models, implementations, prior=prior_metrics)
        self.assertIs(unavailable.best_available, prior_metrics)
        for metric in (
            ModelMetric("third-model", "deep", "benchmark", "USD/month", 5.0),
            ModelMetric("third-model", "deep", "benchmark", "points", float("nan")),
            ModelMetric("absent", "deep", "benchmark", "points", 5.0),
            ModelMetric("third-model", "invented", "benchmark", "points", 5.0),
        ):
            bad = {("fixture-reader", "v3"): FixedReader(prior, Metrics((metric,)))}
            result = fetch_metrics(context, source, prior.models, bad, prior=prior_metrics)
            self.assertEqual(result.status, "failed")
            self.assertIs(result.best_available, prior_metrics)
        self.assertEqual(discover(context, source, implementations).status, "ok")
        for inactive in (replace(source, enabled=False), replace(source, provenance=replace(source.provenance, lifecycle_state="retired"))):
            self.assertEqual(discover(context, inactive, {}, prior=prior).status, "disabled")
            self.assertIs(fetch_metrics(context, inactive, prior.models, {}, prior=prior_metrics).best_available, prior_metrics)
        self.assertEqual(discover(context, source, implementations, prior=prior).status, "ok")
        invented = Discovery(prior.models, HarnessList("third-cli", "ok", (), "fixture", "", "", ""))
        outcome = discover(context, source, {("fixture-reader", "v3"): FixedReader(invented, Metrics(()))}, prior=prior)
        self.assertIn("unknown_execution_runtime", outcome.failure or "")

    def test_t3_builtin_harness_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            calls: list[tuple[str, ...]] = []

            def runner(argv: Sequence[str], timeout: float) -> ProcessResult:
                calls.append(tuple(argv))
                body = "codex-fixture" if argv[-1] == "--version" else json.dumps({"models": [{"slug": "exact-launch.id", "visibility": "list", "supported_reasoning_levels": [{"effort": "deep"}]}]})
                return ProcessResult(0, body, "")

            context = MetadataContext(HarnessContext(home, Path(sys.executable), runner, NOW))
            source = registration(implementation_id="harness.codex", implementation_version="1", aliases={"exact-launch.id": "must-not-rewrite"})
            outcome = discover(context, source, HARNESS_IMPLEMENTATIONS)
            assert outcome.observed is not None and outcome.observed.launchability is not None
            listing = outcome.observed.launchability
            self.assertEqual(outcome.status, "ok")
            self.assertEqual(outcome.observed.models[0].model_id, "exact-launch.id")
            self.assertIsNone(outcome.observed.models[0].context_window_tokens)
            self.assertEqual(listing.client_version, "codex-fixture")
            self.assertEqual(calls, [(sys.executable, "--version"), (sys.executable, "debug", "models")])
            self.assertEqual(listing.source, f"{sys.executable} debug models")
            cache = home / ".codex" / "models_cache.json"
            cache.parent.mkdir()
            cache.write_text(json.dumps({"models": [{"slug": "cached-model", "visibility": "list"}], "client_version": "old-client", "fetched_at": "2026-10-01T00:00:00Z"}))

            def failed_runner(argv: Sequence[str], timeout: float) -> ProcessResult:
                return ProcessResult(9, "", "raw launcher failure")

            degraded = discover(MetadataContext(replace(context.harness, run=failed_runner)), source, HARNESS_IMPLEMENTATIONS)
            assert degraded.observed is not None and degraded.observed.launchability is not None
            self.assertEqual(degraded.status, "degraded")
            self.assertEqual(degraded.observed.launchability.client_version, "old-client")
            self.assertEqual(degraded.observed.launchability.fetched_at, "2026-10-01T00:00:00Z")
            self.assertIn("raw launcher failure", degraded.observed.launchability.detail)
            self.assertTrue(degraded.advisories)
            claude_dir = home / ".claude" / "cache" / "model-catalog"
            claude_dir.mkdir(parents=True)
            for surface in ("cc", "api"):
                (claude_dir / f"{surface}.json").write_text(json.dumps({"version": 2, "fetchedAt": NOW.timestamp() * 1000, "catalog": {"surface": surface, "config": {"models": [{"id": f"{surface}-exact-id", "thinking": {"type": "none"}}]}}}))
            claude = discover(context, registration(implementation_id="harness.claude_code", implementation_version="1"), HARNESS_IMPLEMENTATIONS)
            assert claude.observed is not None and claude.observed.launchability is not None
            self.assertEqual(claude.observed.models[0].model_id, "cc-exact-id")
            self.assertEqual(claude.observed.models[0].efforts, ("non_reasoning",))
            self.assertEqual(claude.observed.launchability.ignored_surfaces, ("api:api.json",))
            self.assertEqual(dict(JOIN_ALIASES), {("anthropic", "Claude 4.5 Haiku"): "claude-haiku-4-5-20251001"})
            only_launch = fetch_metrics(context, source, outcome.observed.models, HARNESS_IMPLEMENTATIONS)
            assert only_launch.observed is not None
            self.assertEqual(only_launch.observed.observations, ())
            self.assertIn("harness_launchability_only", only_launch.advisories[0])


if __name__ == "__main__":
    unittest.main()
