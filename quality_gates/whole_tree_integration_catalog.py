#!/usr/bin/env python3
"""Imported source catalog behind the whole-tree integration gate's C1/C2 key parity.

The gate's decorator-literal AST surfaces cannot see an action that a class
decorator generates, that a parent class supplies, or that is registered
under a constant ``name=``. This module measures what the runtime itself
registers instead, still with no live solet, no plugin constructor and no
database:

* plugins: every completed class behind an ``ananta.plugins`` entry point that
  the tree's ``plugins/*/pyproject.toml`` declares, imported from the tree
  under test and projected by the shared ``registration_census``. The
  provider is the entry-point name, which is what ``PluginManager`` keys by.
* services: the real ``ServiceInterfaceScanner``, enabled predicate included.
  The module import errors it logs and swallows are captured as lost
  coverage, so a failed import can never read as a smaller complete catalog.

Each key is joined to the one JSON path ``KnowledgeBaseOverlayLoader`` reads
for it, and each payload is joined back by its full ``process_key``, never by
filename stem. A provider the catalog cannot measure is an advisory, never a
manufactured finding. Instance-only actions and ``get_available_actions``
overrides stay unmeasured: this is a source catalog, not a deployed-registry
census.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import logging
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Generator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

ENTRY_POINT_GROUP = "ananta.plugins"
CATALOG_LIMITS = "instance-only actions and get_available_actions overrides unmeasured; a source catalog, not a deployed-registry census"
_PROCESS_KEY_RE = re.compile(r"([A-Za-z_]\w*)::([A-Za-z_]\w*)::([A-Za-z_]\w*)")


@dataclass(frozen=True)
class CatalogSpec:
    """One namespace's check ids, payload corpus glob and payload-owner directory level."""

    namespace: str
    missing: str
    mismatch: str
    unknown: str
    duplicate: str
    corpus: str
    owner_parent: int


SERVICE_SPEC = CatalogSpec("service_interface", "C1.5", "C1.6", "C1.7", "C1.8", "ananta/knowledge_base/processes/*/*.json", 0)
PLUGIN_SPEC = CatalogSpec("plugin", "C2.2", "C2.3", "C2.4", "C2.5", "plugins/*/knowledge_base/processes/*.json", 2)


@dataclass(frozen=True)
class ParityFinding:
    """One C1/C2 finding; ``file_path`` is relative to the tree under test."""

    check_id: str
    scope_qualifier: str
    specifier: str
    message: str
    file_path: str


@dataclass
class ProcessCatalog:
    """Registrable full keys of one namespace, each with the JSON path the runtime overlay reads.

    ``unmeasured`` names providers whose surface could not be projected, and
    ``complete`` is False when the namespace itself was only partly scanned.
    A payload either one leaves undecidable becomes an advisory, never a
    finding, and the catalog then never claims completeness.
    """

    spec: CatalogSpec
    root: Path
    entries: dict[str, Path] = field(default_factory=dict)
    providers: set[str] = field(default_factory=set)
    unmeasured: set[str] = field(default_factory=set)
    complete: bool = True
    advisories: list[str] = field(default_factory=list)

    def add(self, process_key: str, json_path: Path) -> None:
        """Record one registrable key and the JSON file the overlay reads for it."""
        self.entries[process_key] = json_path
        self.providers.add(process_key.split("::")[1])

    def is_measured(self, provider: str) -> bool:
        """True when a payload naming ``provider`` can be judged against this catalog."""
        return provider not in self.unmeasured and (self.complete or provider in self.providers)

    def is_complete(self) -> bool:
        """True only when every provider and the whole namespace were measured."""
        return self.complete and not self.unmeasured

    def lose(self, provider: str, reason: str) -> None:
        """Disclose a provider the catalog cannot see; its payloads become unverified."""
        self.unmeasured.add(provider)
        self.advisories.append(f"coverage lost: {self.spec.namespace} provider {provider!r}: {reason}")

    def rel(self, path: Path) -> str:
        """``path`` relative to the tree under test when it lies inside it."""
        return path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else path.as_posix()

    def summary(self) -> str:
        """Key, provider and unmeasured-provider counts for the CATALOG line."""
        return f"{self.spec.namespace} {len(self.entries)} keys from {len(self.providers)} providers, {len(self.unmeasured)} unmeasured"


class _ErrorCapture(logging.Handler):
    """Keep ERROR records as messages, so a logged traceback never reaches the gate output."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store the formatted message only, never the traceback."""
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _captured_errors(logger: logging.Logger) -> Generator[list[str]]:
    """Divert ``logger``'s ERROR records into a list for the duration of the block."""
    handler = _ErrorCapture()
    propagate = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate


def _declared_entry_points(catalog: ProcessCatalog) -> list[tuple[str, str]]:
    """``(provider, "module:attribute")`` for every ``ananta.plugins`` entry point the tree declares."""
    declared: list[tuple[str, str]] = []
    for manifest in sorted(catalog.root.glob("plugins/*/pyproject.toml")):
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            group = cast(dict[str, object], data.get("project", {}).get("entry-points", {}).get(ENTRY_POINT_GROUP, {}))
            declared.extend((provider, str(target)) for provider, target in group.items())
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, AttributeError) as exc:
            catalog.complete = False
            catalog.advisories.append(f"coverage lost: unreadable plugin manifest {catalog.rel(manifest)}: {type(exc).__name__}: {exc}")
    return declared


def _load_plugin_class(catalog: ProcessCatalog, provider: str, target: str) -> type[object] | None:
    """The completed class behind one entry point, imported from the tree and never constructed."""
    from ananta.core.plugins.plugin_base import PluginBase

    module_name, _, attribute = target.partition(":")
    try:
        loaded: object = getattr(importlib.import_module(module_name), attribute)
    except Exception as exc:  # noqa: BLE001 — an import failure is lost coverage, never a gate crash
        return catalog.lose(provider, f"{target} did not import: {type(exc).__name__}: {exc}")
    if not isinstance(loaded, type) or not issubclass(loaded, PluginBase):
        return catalog.lose(provider, f"{target} is not a PluginBase class, so runtime discovery skips it")
    if not Path(inspect.getfile(loaded)).resolve().is_relative_to(catalog.root):
        return catalog.lose(provider, f"{target} imported from {inspect.getfile(loaded)}, outside the tree under test")
    if inspect.getattr_static(loaded, "get_available_actions") is not inspect.getattr_static(PluginBase, "get_available_actions"):
        return catalog.lose(provider, f"{target} overrides get_available_actions, so its runtime action set is dynamic")
    return loaded


def build_plugin_catalog(root: Path) -> ProcessCatalog:
    """Project every declared plugin class the tree imports, keyed at the overlay's JSON path.

    The tree's own import roots go first on ``sys.path``: a lane worktree's
    shared ``.venv`` points its editable installs at a different checkout.
    """
    from ananta.core.process_registry.registration_census import class_registration_census, plugin_process_key

    catalog = ProcessCatalog(PLUGIN_SPEC, root.resolve())
    sys.path[:0] = [str(path) for path in (catalog.root / "ananta" / "src", *sorted(catalog.root.glob("plugins/*/src")))]
    for provider, target in _declared_entry_points(catalog):
        plugin_class = _load_plugin_class(catalog, provider, target)
        if plugin_class is None:
            continue
        kb_dir = Path(inspect.getfile(plugin_class)).resolve().parents[2] / "knowledge_base" / "processes"
        for action in class_registration_census(plugin_class, provider).actions:
            catalog.add(plugin_process_key(provider, action), kb_dir / f"{action.name}.json")
    return catalog


def build_service_catalog(root: Path) -> ProcessCatalog:
    """Register service-interface keys with the real ``ServiceInterfaceScanner``."""
    from ananta.core.process_registry import service_interface_scanner as scanner
    from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator

    catalog = ProcessCatalog(SERVICE_SPEC, root.resolve())
    scanned = Path(scanner.__file__).resolve().parents[2] / "services"
    if scanned != catalog.root / "ananta" / "src" / "ananta" / "services":
        catalog.complete = False
        catalog.advisories.append(f"coverage lost: the service scanner reads {scanned}, outside the tree under test")
        return catalog
    registry: dict[str, object] = {"processes": {}}
    with _captured_errors(scanner.logger) as errors:
        scanner.ServiceInterfaceScanner(InvocationSchemaGenerator()).scan(registry)
    catalog.complete = not errors
    catalog.advisories.extend(f"coverage lost: the service scanner logged: {error}" for error in errors)
    catalog.advisories.extend(f"unsupported path: {catalog.rel(path)} is unscanned; the scanner looks for plugin interfaces under ananta_plugins/" for path in sorted(catalog.root.glob("plugins/*/src/*/interfaces/public.py")))
    kb_dir = catalog.root / "ananta" / "knowledge_base" / "processes"
    for process_key in cast(dict[str, object], registry["processes"]):
        _, provider, function_name = process_key.split("::")
        catalog.add(process_key, kb_dir / provider / f"{function_name}.json")
    return catalog


def collect_payload_keys(paths: Iterable[Path]) -> dict[Path, str | None]:
    """Each KB JSON's payload ``process_key`` as text ("" when absent), or None when unreadable."""
    payloads: dict[Path, str | None] = {}
    for path in paths:
        try:
            payloads[path.resolve()] = str(json.loads(path.read_text(encoding="utf-8")).get("process_key", ""))
        except (OSError, UnicodeDecodeError, ValueError, AttributeError):
            payloads[path.resolve()] = None
    return payloads


def check_parity(catalog: ProcessCatalog, payloads: dict[Path, str | None]) -> list[ParityFinding]:
    """Forward, reverse and duplicate findings for one namespace, each naming a full key and path."""
    found = [_forward_finding(catalog, key, path, payloads) for key, path in sorted(catalog.entries.items())]
    found += [_reverse_finding(catalog, path, payloads[path]) for path in sorted(set(payloads) - set(catalog.entries.values()))]
    counts = Counter(payloads.values())
    found += [_duplicate_finding(catalog, key, payloads) for key in sorted(filter(None, counts)) if counts[key] > 1]
    return [finding for finding in found if finding is not None]


def _forward_finding(catalog: ProcessCatalog, key: str, json_path: Path, payloads: dict[Path, str | None]) -> ParityFinding | None:
    """Missing when the overlay finds no file for a registrable key; mismatch when that file disagrees."""
    _, provider, verb = key.split("::")
    if json_path not in payloads:
        return ParityFinding(catalog.spec.missing, provider, verb, f"{key} is registrable but the runtime overlay finds no KB JSON at {catalog.rel(json_path)}", catalog.rel(json_path))
    if payloads[json_path] != key:
        return ParityFinding(catalog.spec.mismatch, provider, verb, f"{catalog.rel(json_path)} has process_key={payloads[json_path]!r}; the runtime overlay expects {key!r} there", catalog.rel(json_path))
    return None


def _reverse_finding(catalog: ProcessCatalog, path: Path, key: str | None) -> ParityFinding | None:
    """Unknown when no registrable key matches; mismatch when the overlay can never read this file."""
    match = _PROCESS_KEY_RE.fullmatch(key or "")
    if match is None or match.group(1) != catalog.spec.namespace or key in catalog.entries:
        return ParityFinding(catalog.spec.mismatch, path.parents[catalog.spec.owner_parent].name, path.stem, f"{catalog.rel(path)} has process_key={key!r}, which the runtime overlay never reads from this file", catalog.rel(path))
    if not catalog.is_measured(match.group(2)):
        catalog.advisories.append(f"unverified: {catalog.rel(path)} carries {key}; provider {match.group(2)!r} was not measured")
        return None
    return ParityFinding(catalog.spec.unknown, match.group(2), match.group(3), f"{catalog.rel(path)} carries {key}, which no registrable key in the imported {catalog.spec.namespace} catalog matches", catalog.rel(path))


def _duplicate_finding(catalog: ProcessCatalog, key: str, payloads: dict[Path, str | None]) -> ParityFinding:
    """One identity carried by more than one payload."""
    paths = sorted(catalog.rel(path) for path, carried in payloads.items() if carried == key)
    _, provider, verb = (key.split("::") + ["", ""])[:3]
    return ParityFinding(catalog.spec.duplicate, provider, verb, f"{key} is carried by {len(paths)} payloads: {', '.join(paths)}", paths[0])


def catalog_summary(catalogs: list[ProcessCatalog], bound_providers: set[str]) -> str:
    """The CATALOG disclosure line: counts, bound plugin namespaces, and an honest completeness verdict."""
    plugin_providers = set[str]().union(*(c.providers for c in catalogs if c.spec.namespace == PLUGIN_SPEC.namespace))
    status = "complete" if all(map(ProcessCatalog.is_complete, catalogs)) else "INCOMPLETE"
    return f"CATALOG: {'; '.join(map(ProcessCatalog.summary, catalogs))}; {len(bound_providers & plugin_providers)} bound plugin providers keep JSON parity although runtime omits their plugin:: namespace under these bindings; coverage {status} ({CATALOG_LIMITS})"
