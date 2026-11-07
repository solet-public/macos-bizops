"""Registration-selected metadata readers, independent of execution eligibility.

A5 consumes this boundary on each catalog run. Installed implementations are
code; provider/source, version, endpoint, credential reference, aliases and
metric units are A2 data read at use time. No provider-name selector branches
or numeric policy defaults live here. Metadata alone never declares a runtime.

Readers do not write accepted facts. Each outcome retains the registration
revision, raw source failure and prior accepted facts; consumers retain those
outcomes in their existing append-only history. Corrections, clear, retire and
reopen belong to A2's authorized, expected-revision registration lifecycle.
Replacing the injected implementation map replaces the installed parser; no
global mutable registry, credential value or persistent cache is introduced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from math import isfinite
from types import MappingProxyType
from typing import Literal, Protocol

from .model_capability_harness import RUNTIME_ADAPTERS, HarnessContext, HarnessList, RuntimeAdapter
from .model_catalog_policy import CatalogSource


class SourceError(ValueError):
    """An installed reader cannot parse or obtain this source; detail stays raw."""


@dataclass(frozen=True, slots=True)
class MetadataContext:
    """Injected local harness dependencies; external readers own their transport."""

    harness: HarnessContext


@dataclass(frozen=True, slots=True)
class OfferedModel:
    """Provider metadata, not a launch authorization; unknown context stays null."""

    model_id: str
    efforts: tuple[str, ...]
    context_window_tokens: int | None


@dataclass(frozen=True, slots=True)
class ModelMetric:
    """One native metric/unit observation; no price or benchmark normalization."""

    model_id: str
    effort: str | None
    metric: str
    unit: str
    value: float


@dataclass(frozen=True, slots=True)
class Discovery:
    models: tuple[OfferedModel, ...]
    launchability: HarnessList | None = None
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Metrics:
    observations: tuple[ModelMetric, ...]
    advisories: tuple[str, ...] = ()


class MetadataAdapter(Protocol):
    """Installed parser selected by exact registration implementation ID/version."""

    def discover(self, context: MetadataContext, registration: CatalogSource) -> Discovery: ...

    def fetch_metrics(self, context: MetadataContext, registration: CatalogSource, offered_models: Sequence[OfferedModel]) -> Metrics: ...


@dataclass(frozen=True, slots=True)
class SourceOutcome[T]:
    registration: CatalogSource
    status: Literal["ok", "degraded", "failed", "disabled"]
    observed: T | None
    prior: T | None
    failure: str | None
    advisories: tuple[str, ...]

    @property
    def best_available(self) -> T | None:
        """A failed source cannot erase its accepted predecessor or another source."""
        return self.observed if self.observed is not None else self.prior


def _resolve(registration: CatalogSource, implementations: Mapping[tuple[str, str], MetadataAdapter]) -> MetadataAdapter:
    key = (registration.implementation_id, registration.implementation_version)
    adapter = implementations.get(key)
    if adapter is None:
        raise SourceError(f"adapter_unavailable: {key!r}; source {registration.source_id!r}")
    return adapter


def _inactive(registration: CatalogSource) -> bool:
    return not registration.enabled or registration.provenance.lifecycle_state != "active"


def _disabled[T](registration: CatalogSource, prior: T | None) -> SourceOutcome[T]:
    return SourceOutcome(registration, "disabled", None, prior, None, ("source registration disabled or retired; prior facts retained, not newly eligible",))


def _failed[T](registration: CatalogSource, prior: T | None, error: Exception) -> SourceOutcome[T]:
    detail = f"{type(error).__name__}: {error}"
    return SourceOutcome(registration, "failed", None, prior, detail, (f"source_failed: {registration.source_id}: {detail}; accepted facts retained",))


def discover(context: MetadataContext, registration: CatalogSource, implementations: Mapping[tuple[str, str], MetadataAdapter], *, prior: Discovery | None = None) -> SourceOutcome[Discovery]:
    """Read current registration data; unavailable sources remain independent outcomes."""
    if _inactive(registration):
        return _disabled(registration, prior)
    try:
        found = _resolve(registration, implementations).discover(context, registration)
        _validate_discovery(found)
        # Launch IDs are verbatim harness authority. Aliases apply only to metadata.
        models = found.models if found.launchability is not None else tuple(replace(model, model_id=registration.aliases.get(model.model_id, model.model_id)) for model in found.models)
        found = replace(found, models=models)
    except (OSError, ValueError) as exc:
        return _failed(registration, prior, exc)
    status: Literal["ok", "degraded"] = "degraded" if found.launchability is not None and found.launchability.status != "ok" else "ok"
    return SourceOutcome(registration, status, found, prior, None, found.advisories)


def _validate_discovery(found: Discovery) -> None:
    for model in found.models:
        if not model.model_id or (model.context_window_tokens is not None and (isinstance(model.context_window_tokens, bool) or model.context_window_tokens <= 0)):
            raise SourceError("invalid_model_metadata: model identity and positive context window required when known")
    if found.launchability is not None and found.launchability.runtime not in RUNTIME_ADAPTERS:
        raise SourceError(f"unknown_execution_runtime: {found.launchability.runtime!r}; metadata cannot install a runtime")


def fetch_metrics(context: MetadataContext, registration: CatalogSource, offered_models: Sequence[OfferedModel], implementations: Mapping[tuple[str, str], MetadataAdapter], *, prior: Metrics | None = None) -> SourceOutcome[Metrics]:
    """Preserve native metric units; malformed observations fail only their source."""
    if _inactive(registration):
        return _disabled(registration, prior)
    try:
        found = _resolve(registration, implementations).fetch_metrics(context, registration, offered_models)
        observations = tuple(replace(item, model_id=registration.aliases.get(item.model_id, item.model_id)) for item in found.observations)
        _validate_metrics(registration, offered_models, observations)
        found = replace(found, observations=observations)
    except (OSError, ValueError) as exc:
        return _failed(registration, prior, exc)
    return SourceOutcome(registration, "ok", found, prior, None, found.advisories)


def _validate_metrics(registration: CatalogSource, offered: Sequence[OfferedModel], observations: Sequence[ModelMetric]) -> None:
    models = {model.model_id: model for model in offered}
    for item in observations:
        if registration.metric_units.get(item.metric) != item.unit or not item.unit:
            raise SourceError(f"metric_unit_mismatch: {item.metric!r} in {item.unit!r}; registration declares {registration.metric_units.get(item.metric)!r}")
        if isinstance(item.value, bool) or not isfinite(item.value):
            raise SourceError(f"invalid_metric_value: {item.metric!r} must be finite")
        model = models.get(item.model_id)
        if model is None or (item.effort is not None and item.effort not in model.efforts):
            raise SourceError(f"metric_model_not_offered: {item.model_id!r}/{item.effort!r}")


@dataclass(frozen=True, slots=True)
class HarnessMetadataAdapter:
    """Expose an existing launch reader without rewriting its IDs or provenance."""

    adapter: RuntimeAdapter

    def discover(self, context: MetadataContext, registration: CatalogSource) -> Discovery:
        listing = self.adapter.read_harness(context.harness)
        if listing.status in {"unreadable", "empty"}:
            raise SourceError(f"harness_{listing.status}: {listing.source}: {listing.detail}; {listing.advisories!r}")
        models = tuple(OfferedModel(model.model_id, model.efforts, None) for model in listing.offered_models)
        return Discovery(models, listing, listing.advisories)

    def fetch_metrics(self, context: MetadataContext, registration: CatalogSource, offered_models: Sequence[OfferedModel]) -> Metrics:
        return Metrics((), ("harness_launchability_only: benchmark metrics and prices require a registered metadata source",))


HARNESS_IMPLEMENTATIONS: Mapping[tuple[str, str], MetadataAdapter] = MappingProxyType({adapter.implementation_key: HarnessMetadataAdapter(adapter) for adapter in RUNTIME_ADAPTERS.values()})
