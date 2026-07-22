"""Manifest parser + working-tree classifier.

See ``workbench/2026-06-16_root_manifest_yaml_design.md`` §3 + §6.2.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterable
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft7Validator

from .types import (
    Classification,
    Manifest,
    SunsetOverdue,
)


class ManifestLoadError(RuntimeError):
    """Raised when the manifest file cannot be opened / parsed as YAML."""


def _load_schema() -> dict[str, Any]:
    schema_text = files(__package__).joinpath("schema.json").read_text(encoding="utf-8")
    return cast(dict[str, Any], json.loads(schema_text))


def _validate_schema(payload: dict[str, Any]) -> str | None:
    """Return ``None`` on pass, else a single-line human-readable error."""
    validator = Draft7Validator(_load_schema())
    errors = sorted(validator.iter_errors(cast(Any, payload)), key=lambda err: err.path)
    if not errors:
        return None
    first = errors[0]
    location = "/".join(str(p) for p in first.absolute_path) or "<root>"
    return f"manifest schema violation at {location}: {first.message}"


def _coerce_manifest(payload: dict[str, Any]) -> Manifest:
    return Manifest(
        schema_version=int(payload["schema_version"]),
        homunculus_name=str(payload["homunculus_name"]),
        universal_files=tuple(payload["universal"]["files"]),
        universal_directories=tuple(payload["universal"]["directories"]),
        platform_managed_directories=tuple(payload["platform_managed"]["directories"]),
        sanctioned_entries=tuple(payload["sanctioned"]),
        overrides_entries=tuple(payload["overrides"]),
        diagnostic_report_categories=tuple(payload["diagnostic"]["report_categories"]),
        diagnostic_ignore_patterns=tuple(payload["diagnostic"]["ignore_patterns"]),
    )


def load_manifest(manifest_path: Path) -> tuple[Manifest | None, str | None]:
    """Read + validate the manifest.

    Returns ``(manifest, None)`` on success; ``(None, error_message)``
    when the file is missing, the YAML is malformed, or the JSON Schema
    validation fails.  Callers decide whether the error is BLOCKING
    (pre-commit, cutover) or NEVER-BLOCKING (diagnostic).
    """
    if not manifest_path.is_file():
        return None, f"root_manifest.yaml not found at {manifest_path}"
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, f"root_manifest.yaml is not parseable YAML: {exc}"
    if not isinstance(raw, dict):
        return None, "root_manifest.yaml top-level must be a mapping"
    schema_err = _validate_schema(raw)
    if schema_err is not None:
        return None, schema_err
    return _coerce_manifest(raw), None


def _matches_any_pattern(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _scan_root(
    repo_root: Path,
    platform_managed: Iterable[str],
    ignore_patterns: Iterable[str],
) -> set[str]:
    """Enumerate first-level repo_root entries, dropping platform-managed
    cascade names + diagnostic ignore patterns.

    Platform-managed names match on basename at any depth — the manifest
    declares the pattern (e.g. ``__pycache__``), the scanner just skips it.
    """
    managed_set = {entry.rstrip("/") for entry in platform_managed}
    entries: set[str] = set()
    for child in repo_root.iterdir():
        name = child.name
        if name in managed_set:
            continue
        if _matches_any_pattern(name, ignore_patterns):
            continue
        entries.add(name)
    return entries


def _parse_iso_date(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _collect_sunset_overdue(
    entries: Iterable[dict[str, str]],
    present: set[str],
    today: date,
) -> tuple[tuple[SunsetOverdue, ...], tuple[str, ...]]:
    """Split sanctioned/overrides entries into:

    * ``sunset_overdue`` — entries whose date sunset is past AND path is
      PRESENT (BLOCKING).
    * ``cleanup_overdue`` — entries whose date sunset is past AND path is
      ABSENT (WARN/INFO — operator should remove from manifest).

    Milestone-string sunsets are deliberately ignored; the consumer can
    only enforce objective date strings (§4.4).
    """
    overdue: list[SunsetOverdue] = []
    cleanup: list[str] = []
    for entry in entries:
        sunset_str = entry.get("sunset_target")
        if not sunset_str:
            continue
        sunset_date = _parse_iso_date(sunset_str)
        if sunset_date is None or sunset_date >= today:
            continue
        path = entry["path"]
        days = (today - sunset_date).days
        normalized = path.rstrip("/")
        if normalized in present:
            overdue.append(SunsetOverdue(path=path, sunset_target=sunset_str, days_overdue=days))
        else:
            cleanup.append(path)
    return tuple(overdue), tuple(cleanup)


def classify_root_entries(
    manifest_path: Path,
    repo_root: Path,
    *,
    today: date | None = None,
    extra_ignore_patterns: Iterable[str] = (),
) -> Classification:
    """Parse the manifest, scan the working tree, return drift facts.

    Diagnostic consumers may pass additional per-developer ignore patterns
    via ``extra_ignore_patterns`` (typically from ``ANANTA_ROOT_MANIFEST_IGNORE_PATTERNS``;
    BLOCKING consumers ignore the env var per §3.7 — they pass ``()``).

    Schema-validation failures land in ``Classification.schema_validation_error``
    and otherwise yield an empty Classification — diagnostic consumers
    continue per §7.6 (NEVER BLOCKING), BLOCKING consumers fail-closed.
    """
    manifest, error = load_manifest(manifest_path)
    if manifest is None:
        return Classification(
            manifest_path=manifest_path,
            repo_root=repo_root,
            homunculus_name=None,
            schema_validation_error=error,
        )

    now = today or date.today()
    ignore_patterns = tuple(manifest.diagnostic_ignore_patterns) + tuple(extra_ignore_patterns)
    present = _scan_root(
        repo_root,
        manifest.platform_managed_directories,
        ignore_patterns,
    )

    declared_universal = set(manifest.universal_files) | {
        d.rstrip("/") for d in manifest.universal_directories
    }
    declared_sanctioned = {entry["path"].rstrip("/") for entry in manifest.sanctioned_entries}
    declared_overrides = {entry["path"].rstrip("/") for entry in manifest.overrides_entries}
    all_declared = declared_universal | declared_sanctioned | declared_overrides

    unknown = sorted(present - all_declared)
    missing_universal = sorted(declared_universal - present)
    missing_sanctioned = sorted(declared_sanctioned - present)

    sanctioned_overdue, sanctioned_cleanup = _collect_sunset_overdue(
        manifest.sanctioned_entries, present, now,
    )
    overrides_overdue, overrides_cleanup = _collect_sunset_overdue(
        manifest.overrides_entries, present, now,
    )

    return Classification(
        manifest_path=manifest_path,
        repo_root=repo_root,
        homunculus_name=manifest.homunculus_name,
        unknown_entries=tuple(unknown),
        missing_universal=tuple(missing_universal),
        missing_sanctioned=tuple(missing_sanctioned),
        sunset_overdue=sanctioned_overdue + overrides_overdue,
        cleanup_overdue=sanctioned_cleanup + overrides_cleanup,
    )
