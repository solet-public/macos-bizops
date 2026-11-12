"""Pre-runtime saved-location migration for every previously adopted solet.

The address seed is the durable pending record. This module plans from files
only, writes it atomically through Runtime, and leaves registration to the new
address-book boot loader. Connector configs and existing exports are read-only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ananta.file_locations.documents import default_solet_home, documents_folder, exports_folder
from ananta.file_locations.resolution import SoletTree, overlaps_solet_tree

from .export_root_validation import BUSINESS_CONNECTOR_PLUGINS
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, evidence, planned_action, result
from .setup_adapter_runtime import Runtime


@dataclass(frozen=True, slots=True)
class _ExportRootPlan:
    seed: Path
    content: str
    changed: bool
    folders: tuple[Path, Path]
    roots: int
    advisories: tuple[str, ...]


def migrate_file_locations(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Stage path-keyed addresses before boot; never call runtime or rewrite a connector."""
    plan = _export_root_plan(request, runtime)
    advisories = list(plan.advisories)
    if request.phase == "probe":
        actions = _export_root_actions(plan)
        return result(request, status="pending" if actions else "verified", evidence_items=_export_root_evidence(plan, advisories), actions=actions, repair="Apply the saved-location migration." if actions else None)
    for directory in plan.folders:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            advisories.append(_folder_access_advisory(directory, request, exc))
    if plan.changed:
        # Runtime.atomic_write fsyncs a same-folder temporary file, renames it,
        # and fsyncs the directory. The seed itself is the pending marker.
        runtime.atomic_write(plan.seed, plan.content, mode=plan.seed.stat().st_mode & 0o777 if plan.seed.exists() else 0o600)
    return result(request, status="applied", retry_safe=True, evidence_items=_export_root_evidence(plan, advisories))


def _expanded_root(value: str, home: Path) -> Path | None:
    expanded = str(home) + value[1:] if value == "~" or value.startswith("~/") else os.path.expanduser(value)
    directory = Path(os.path.expandvars(expanded))
    return Path(os.path.realpath(directory)) if value.strip() and directory.is_absolute() else None


def _configured_roots(config_dir: Path, home: Path, advisories: list[str]) -> dict[Path, set[str]]:
    roots: dict[Path, set[str]] = {}
    for plugin in BUSINESS_CONNECTOR_PLUGINS:
        config_path = config_dir / f"{plugin}.json"
        config = _read_connector_config(config_path, advisories)
        if config is None:
            continue
        keys = ("export_allowed_roots", "import_allowed_roots") if plugin == "salesforce_plugin" else ("export_allowed_roots",)
        for key in keys:
            values = config.get(key, [])
            if not isinstance(values, list):
                advisories.append(f"{plugin} {key} is not a list; the configured value is untouched.")
                continue
            for value in values:
                _collect_root_value(roots, plugin, key, value, home, advisories)
    return roots


def _collect_root_value(roots: dict[Path, set[str]], plugin: str, key: str, value: JsonValue, home: Path, advisories: list[str]) -> None:
    directory = _expanded_root(value, home) if isinstance(value, str) else None
    if directory is not None:
        roots.setdefault(directory, set()).add(plugin)
        return
    label = "blank" if isinstance(value, str) and not value.strip() else repr(value)
    advisories.append(f"{plugin} {key} root {label} is not an absolute directory; the configured value is untouched.")


def _pending_directory(entry: JsonObject, home: Path) -> Path | None:
    fields = entry.get("entries", [])
    if not isinstance(fields, list):
        return None
    for field in fields:
        if isinstance(field, dict) and field.get("field_type") == "path" and isinstance(value := field.get("value"), str):
            return _expanded_root(value, home)
    return None


def _new_path_seed(name: str, directory: Path, tags: list[str], *, address_type: str = "path") -> JsonObject:
    return {"name": name, "address_type": address_type, "description": "Saved file location", "tags": cast(JsonValue, tags), "entries": [{"field_type": "path", "description": "Directory", "value": str(directory)}]}


def _pending_payload(seed: Path) -> tuple[str, JsonObject, list[JsonObject]]:
    original = seed.read_text(encoding="utf-8") if seed.exists() else ""
    payload: JsonValue = json.loads(original) if seed.exists() else {"entries": []}
    if not isinstance(payload, dict) or not isinstance(entries := payload.get("entries"), list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError(f"Preserving invalid address seed {seed}; correct its entries list before updating.")
    return original, payload, cast(list[JsonObject], entries)


def _home_seed_folders(request: AdapterRequest, runtime: Runtime, entries: list[JsonObject], advisories: list[str]) -> tuple[Path, Path]:
    tree = SoletTree(request.target, request.target / "profile")
    documents = documents_folder(sys.platform, os.environ, runtime.home)
    default = default_solet_home(request.name, documents, tree, runtime.home / "Library/Application Support" / request.name / "Files")
    advisories.extend(default.advisories)
    by_name = {entry.get("name"): entry for entry in entries if isinstance(entry.get("name"), str)}
    home = _pending_directory(by_name.get("solet_home", {}), runtime.home) or default.value
    if overlaps_solet_tree(home, tree):
        advisories.append(f"Existing solet_home {home} overlaps the solet tree; using {default.value} and preserving the saved entry.")
        home = default.value
    exports = _pending_directory(by_name.get("solet_home_exports", {}), runtime.home) or exports_folder(home)
    if overlaps_solet_tree(exports, tree):
        advisories.append(f"Existing exports folder {exports} overlaps the solet tree; using {exports_folder(home)} and preserving the saved entry.")
        exports = exports_folder(home)
    for name, directory, tag in (("solet_home", home, "solet_home"), ("solet_home_exports", exports, "exports")):
        if name not in by_name:
            entries.append(_new_path_seed(name, directory, [tag]))
    return home, exports


def _carry_root_seeds(entries: list[JsonObject], roots: dict[Path, set[str]], runtime: Runtime, tree: SoletTree, exports: Path, advisories: list[str]) -> None:
    by_path = {directory: entry for entry in entries if (directory := _pending_directory(entry, runtime.home)) is not None}
    number = _next_seed_number(entries)
    for directory, plugins in sorted(roots.items()):
        tags, address_type = _root_seed_metadata(directory, plugins, tree, exports, advisories)
        if directory in by_path:
            _merge_pending_tags(by_path[directory], tags)
            continue
        entries.append(_new_path_seed(f"migrated_export_root_{number}", directory, tags, address_type=address_type))
        number += 1


def _next_seed_number(entries: list[JsonObject]) -> int:
    numbers = [int(match[1]) for entry in entries if isinstance(name := entry.get("name"), str) and (match := re.fullmatch(r"migrated_export_root_(\d+)", name))]
    return max(numbers, default=0) + 1


def _root_seed_metadata(directory: Path, plugins: set[str], tree: SoletTree, exports: Path, advisories: list[str]) -> tuple[list[str], str]:
    tags = ["migrated", *(f"from:{plugin}" for plugin in sorted(plugins))]
    if overlaps_solet_tree(directory, tree):
        advisories.append(f"Old export folder {directory} overlaps the solet tree; exports now go to {exports}; register another folder with 'save exports in <folder>'.")
        return tags, "path_inside_solet_tree"
    if not directory.exists():
        tags.append("missing")
        advisories.append(f"Old export folder {directory} is missing on disk; its saved location is preserved.")
    return tags, "path"


def _merge_pending_tags(entry: JsonObject, tags: list[str]) -> None:
    existing = entry.get("tags", [])
    if not isinstance(existing, list) or any(not isinstance(tag, str) for tag in existing):
        raise ValueError(f"Preserving invalid tags on saved location {entry.get('name')}; correct them before updating.")
    entry["tags"] = cast(JsonValue, list(dict.fromkeys([*cast(list[str], existing), *tags])))


def _export_root_plan(request: AdapterRequest, runtime: Runtime) -> _ExportRootPlan:
    config_dir = request.target / "profile/config/plugins"
    seed = config_dir / "default_address_book_plugin/entries.json"
    original, payload, entries = _pending_payload(seed)
    before = json.dumps(payload, sort_keys=True)
    advisories: list[str] = []
    roots = _configured_roots(config_dir, runtime.home, advisories)
    folders = _home_seed_folders(request, runtime, entries, advisories)
    _carry_root_seeds(entries, roots, runtime, SoletTree(request.target, request.target / "profile"), folders[1], advisories)
    changed = not original or json.dumps(payload, sort_keys=True) != before
    content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n" if changed else original
    return _ExportRootPlan(seed, content, changed, folders, len(roots), tuple(advisories))


def _export_root_actions(plan: _ExportRootPlan) -> list[JsonObject]:
    actions = [planned_action(action_id="export_root.address_seeds", title="Keep old export folders as saved locations", mutation_kind="file_write", target=str(plan.seed), evidence_ref="migration.export_root_containment")] if plan.changed else []
    for number, directory in enumerate(plan.folders):
        if plan.changed and not directory.is_dir():
            actions.append(planned_action(action_id=f"export_root.home_{number}", title="Create the solet's files folder", mutation_kind="directory_create", target=str(directory), evidence_ref="migration.export_root_containment"))
    return actions


def _folder_access_advisory(directory: Path, request: AdapterRequest, error: OSError) -> str:
    remedy = f"Open System Settings → Privacy & Security → Files and Folders (or Full Disk Access) and allow {request.target / '.venv/bin/python3'}." if sys.platform == "darwin" else "Grant folder access or choose another saved location."
    return f"Could not create {directory}: {error}. {remedy} The saved location is kept; the solet keeps working."


def _export_root_evidence(plan: _ExportRootPlan, advisories: list[str]) -> list[JsonObject]:
    items = [
        evidence(
            evidence_id="migration.export_root_containment",
            kind="existing_install",
            status="pending" if plan.changed else "verified",
            summary="Saved locations pending new boot",
            observed=["connector_configs_untouched=true", f"pending_marker={plan.seed}", f"roots={plan.roots}"],
            expected="verified",
            source="migration.export_root_containment",
        )
    ]
    items.extend(evidence(evidence_id=f"export_root.advisory.{number}", kind="existing_install", status="verified", summary="Saved-location advisory", observed=note, expected="advisory only", source="migration.export_root_containment") for number, note in enumerate(advisories))
    return items


def _read_connector_config(path: Path, advisories: list[str]) -> JsonObject | None:
    try:
        payload: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        advisories.append(f"Could not read roots from {path}; the config is untouched. Correct its JSON and run the update again.")
        return None
    if isinstance(payload, dict):
        return payload
    advisories.append(f"Connector config {path} is not an object; the config is untouched.")
    return None
