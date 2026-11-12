"""Address book plugin seed loader — auto-registers entries from a profile JSON file."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ananta.file_locations.resolution import solet_tree_from_app_home
from ananta.interfaces.state_service_protocol import StateServiceProtocol

from .address_ops import guard_path_address, normalise_location_tags, register_impl
from .address_queries import get_by_name, list_path_address_rows
from .constants import ErrorCode
from .result_helpers import now


def _load_seed_payload(app_home: Path, plugin_name: str, logger: logging.Logger) -> list[Any]:
    """Read and validate the seed file structure. Returns the entries list or []."""
    seed_file = app_home / "config" / "plugins" / plugin_name / "entries.json"
    if not seed_file.is_file():
        logger.debug("no address book seed file at %s — skipping auto-seed", seed_file)
        return []
    try:
        payload = json.loads(seed_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{plugin_name}: failed to read seed file {seed_file}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{plugin_name}: seed file {seed_file} must contain a JSON object, got {type(payload).__name__}")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError(f"{plugin_name}: seed file {seed_file} missing 'entries' list")
    return entries  # type: ignore[return-value]


def _validate_field_entry(fe: Any, j: int, entry_name: str, plugin_name: str) -> dict[str, str]:
    """Validate a single field_type/description/value dict. Raises on bad shape."""
    if not isinstance(fe, dict):
        raise RuntimeError(f"{plugin_name}: seed entry '{entry_name}' field #{j} must be an object")
    for required in ("field_type", "description", "value"):
        if required not in fe or not isinstance(fe[required], str):
            raise RuntimeError(f"{plugin_name}: seed entry '{entry_name}' field #{j} missing string field '{required}'")
    return {
        "field_type": fe["field_type"],
        "description": fe["description"],
        "value": fe["value"],
    }


def _validate_tags_field(
    tags_raw: Any,
    entry_name: str,
    plugin_name: str,
) -> None:
    """Raise if tags_raw is not a list of strings."""
    if not isinstance(tags_raw, list) or not all(isinstance(t, str) for t in tags_raw):
        raise RuntimeError(f"{plugin_name}: seed entry '{entry_name}' 'tags' must be a list of strings")


def _validate_required_string_fields(
    name: Any,
    address_type: Any,
    description: Any,
    entries_raw: Any,
    index: int,
    plugin_name: str,
) -> None:
    """Raise if name, address_type, description, or entries_raw fail basic shape checks."""
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"{plugin_name}: seed entry #{index} missing 'name'")
    if not isinstance(address_type, str) or not address_type:
        raise RuntimeError(f"{plugin_name}: seed entry '{name}' missing 'address_type'")
    if not isinstance(description, str):
        raise RuntimeError(f"{plugin_name}: seed entry '{name}' has non-string 'description'")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise RuntimeError(f"{plugin_name}: seed entry '{name}' must have a non-empty 'entries' list")


def _parse_seed_entry(
    raw: Any,
    index: int,
    plugin_name: str,
) -> tuple[str, str, str, list[str], list[dict[str, str]]]:
    """Validate and normalise a single seed entry."""
    if not isinstance(raw, dict):
        raise RuntimeError(f"{plugin_name}: seed entry #{index} in seed file must be an object")

    name = raw.get("name")
    address_type = raw.get("address_type")
    description = raw.get("description", "")
    tags_raw = raw.get("tags", [])
    entries_raw = raw.get("entries")

    _validate_required_string_fields(name, address_type, description, entries_raw, index, plugin_name)
    _validate_tags_field(tags_raw, str(name), plugin_name)
    assert isinstance(entries_raw, list)

    field_entries = [_validate_field_entry(fe, j, str(name), plugin_name) for j, fe in enumerate(entries_raw)]
    return str(name), str(address_type), str(description), list(tags_raw), field_entries


def _path_key(entries: list[dict[str, Any]]) -> str | None:
    """Compare a single absolute path after expansion and symlink resolution."""
    values = [entry.get("value") for entry in entries if entry.get("field_type") == "path"]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    path = Path(os.path.expandvars(os.path.expanduser(values[0])))
    return os.path.realpath(path) if path.is_absolute() else None


def _merge_seed_tags(state_service: StateServiceProtocol, namespace: str, address: dict[str, Any], tags: list[str]) -> None:
    """Merge pending metadata without rewriting an already satisfied marker."""
    existing = address.get("tags", [])
    merged = list(dict.fromkeys([*existing, *normalise_location_tags("path", tags)]))
    if merged == existing:
        return
    result = state_service.update_state(
        namespace=namespace,
        query={"table": "address", "filters": {"id": address["id"]}},
        updates={"tags": merged, "updated_at": now()},
    )
    if result.get("action_status") != "completed":
        raise RuntimeError(f"{namespace}: failed to merge seed tags for {address['name']!r}: {result.get('error')}")


def _available_migration_name(state_service: StateServiceProtocol, namespace: str) -> str:
    number = 1
    while get_by_name(state_service, namespace, f"migrated_export_root_{number}"):
        number += 1
    return f"migrated_export_root_{number}"


def _seed_identity(
    state_service: StateServiceProtocol,
    namespace: str,
    app_home: Path,
    logger: logging.Logger,
    name: str,
    address_type: str,
    tags: list[str],
    entries: list[dict[str, str]],
) -> tuple[str, list[str], bool]:
    """Satisfy a pending path by directory; reserve a fresh name otherwise."""
    if address_type != "path":
        return name, tags, get_by_name(state_service, namespace, name) is not None
    guard = guard_path_address(address_type, entries, solet_tree_from_app_home(app_home))
    if guard is not None:
        logger.warning("Skipping path address %r at boot: %s", name, guard.get("error"))
        return name, tags, True
    key = _path_key(entries)
    for address in list_path_address_rows(state_service, namespace):
        if key is not None and _path_key(address["entries"]) == key:
            _merge_seed_tags(state_service, namespace, address, tags)
            return name, tags, True
    if get_by_name(state_service, namespace, name):
        renamed = _available_migration_name(state_service, namespace)
        logger.warning("Pending folder %s renamed from %r to %r", key, name, renamed)
        return renamed, [*tags, f"renamed_from:{name}"], False
    return name, tags, False


def auto_seed_entries_from_file(
    app_home: Path,
    plugin_name: str,
    logger: logging.Logger,
    state_service: StateServiceProtocol,
    memory_service: Any,
    auto_ingest_enabled: bool,
) -> None:
    """Auto-register entries from a profile-side JSON file, if present.

    File location: ``$APP_HOME/config/plugins/default_address_book_plugin/entries.json``.
    Paths are idempotent by realpath, with metadata merged and name collisions
    preserved under a fresh migrated name. Other address types retain name keys.
    """
    entries = _load_seed_payload(app_home, plugin_name, logger)
    if not entries:
        return

    registered = 0
    skipped = 0
    for index, raw in enumerate(entries):
        name, address_type, description, tags, field_entries = _parse_seed_entry(raw, index, plugin_name)
        name, tags, satisfied = _seed_identity(state_service, plugin_name, app_home, logger, name, address_type, tags, field_entries)
        if satisfied:
            skipped += 1
            continue

        result = register_impl(
            state_service,
            plugin_name,
            memory_service,
            auto_ingest_enabled,
            logger,
            name,
            address_type,
            description,
            field_entries,
            tags,
            tree=solet_tree_from_app_home(app_home),
        )
        if result.get("action_status") != "completed":
            failure = result.get("error") or {}
            if failure.get("code") == ErrorCode.PATH_OVERLAPS_TREE:
                logger.warning("Skipping path address %r at boot: %s", name, failure.get("message"))
                skipped += 1
                continue
            raise RuntimeError(f"{plugin_name}: failed to seed address book entry '{name}': {result.get('error')}")
        registered += 1

    logger.info(
        "Address book auto-seed: registered=%d skipped=%d",
        registered,
        skipped,
    )
