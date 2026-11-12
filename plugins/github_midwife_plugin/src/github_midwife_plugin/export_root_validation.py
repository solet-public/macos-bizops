"""Hydration home addresses; connector configs are never written."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ananta.file_locations.containment import assert_location_admissible
from ananta.file_locations.documents import default_solet_home, documents_folder, exports_folder
from ananta.file_locations.resolution import SoletTree, overlaps_solet_tree

ERROR_EXPORT_ROOT_REJECTED: Final[str] = "github_midwife.export_root_rejected"

# B1 reads this roster to carry existing connector roots into addresses.
BUSINESS_CONNECTOR_PLUGINS: Final[tuple[str, ...]] = (
    "external_postgres_plugin",
    "snowflake_plugin",
    "salesforce_plugin",
    "jira_plugin",
    "g_suite_plugin",
    "zuora_plugin",
    "marketo_plugin",
)


class ExportRootRejectedError(RuntimeError):
    """Raised when a proposed export root fails the mutual-containment check."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERROR_EXPORT_ROOT_REJECTED


def _resolve_absolute(label: str, raw: str) -> str:
    """Expand ``~``/env vars and require the result to be absolute.

    Relative or blank input would resolve against the SERVER process cwd —
    the same implicit-cwd containment hole the connectors' own gate
    prohibits (2026-07-15 ruling).
    """
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if not expanded or not os.path.isabs(expanded):
        raise ExportRootRejectedError(
            f"{label} must be an ABSOLUTE directory path after ~/$VAR expansion (got {raw!r}); a relative or blank path would resolve against the server process cwd",
        )
    return expanded


def assert_export_root_valid(proposed_root: str, app_home: str) -> str:
    """Validate hydration locations using the same core tree guard."""
    directory = Path(_resolve_absolute("the export root", proposed_root))
    tree = SoletTree(Path(app_home).parent, Path(app_home))
    try:
        assert_location_admissible(directory, tree)
    except ValueError as exc:
        raise ExportRootRejectedError(str(exc)) from exc
    return str(directory.resolve())


@dataclass(frozen=True, slots=True)
class SoletHomeResult:
    """Hydration paths and advisories, including any preserved reserved names."""

    solet_home: Path
    exports: Path
    advisories: tuple[str, ...]


def _reserved_path(entries: list[dict[str, Any]], name: str) -> Path | None:
    for address in entries:
        if address.get("name") != name:
            continue
        for entry in address.get("entries", []):
            if entry.get("field_type") == "path" and Path(entry["value"]).is_absolute():
                return Path(entry["value"])
    return None


def _create_home_folders(home: Path, exports: Path, advisories: list[str]) -> None:
    for directory in (home, exports):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            advisories.append(f"Could not create {directory}: {exc}; grant access or choose another folder.")


def _write_home_addresses(seed: Path, payload: dict[str, Any], solet_name: str, home: Path, exports: Path) -> None:
    entries: list[dict[str, Any]] = payload["entries"]
    names = {entry.get("name") for entry in entries}
    for name, directory, tag in (("solet_home", home, "solet_home"), ("solet_home_exports", exports, "exports")):
        if name in names:
            continue
        entries.append({"name": name, "address_type": "path", "description": f"Files for {solet_name}", "tags": [tag], "entries": [{"field_type": "path", "description": "Directory", "value": str(directory)}]})
    seed.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2) + "\n"
    if not seed.is_file() or seed.read_text(encoding="utf-8") != rendered:
        seed.write_text(rendered, encoding="utf-8")


def _chosen_home(chosen: str | None, default: Path, tree: SoletTree, advisories: list[str]) -> Path:
    home = Path(os.path.expandvars(os.path.expanduser(chosen))) if chosen else default
    if not home.is_absolute() or overlaps_solet_tree(home, tree):
        advisories.append(f"Chosen home {home} is relative or overlaps the solet tree; using {default} outside the tree.")
        return default
    return home


def configure_solet_home(clone_root: Path, app_home: str, solet_name: str, chosen: str | None) -> SoletHomeResult:
    """Create the confirmed Documents home and seed its two reserved addresses.

    Existing names are preserved. The boot loader separately preserves names
    already live in the address book. An unsafe answer produces an advisory
    and the computed default; it does not interrupt hydration.
    """
    tree = SoletTree(clone_root, Path(app_home))
    documents = documents_folder(sys.platform, os.environ, Path.home())
    default = default_solet_home(solet_name, documents, tree, Path.home() / "Library/Application Support" / solet_name / "Files")
    advisories: list[str] = list(default.advisories)
    home = _chosen_home(chosen, default.value, tree, advisories)
    seed = Path(app_home) / "config/plugins/default_address_book_plugin/entries.json"
    payload = json.loads(seed.read_text(encoding="utf-8")) if seed.is_file() else {"entries": []}
    entries: list[dict[str, Any]] = payload["entries"]
    existing_home = _reserved_path(entries, "solet_home")
    if existing_home is not None and not overlaps_solet_tree(existing_home, tree):
        if existing_home != home:
            advisories.append(f"Preserving existing solet_home address at {existing_home}.")
        home = existing_home
    exports = exports_folder(home)
    existing_exports = _reserved_path(entries, "solet_home_exports")
    if existing_exports is not None and not overlaps_solet_tree(existing_exports, tree):
        exports = existing_exports
    _create_home_folders(home, exports, advisories)
    _write_home_addresses(seed, payload, solet_name, home, exports)
    if not documents.proven:
        advisories.append(f"Documents resolution on {sys.platform} is unproven; default is {documents.path}.")
    return SoletHomeResult(home, exports, tuple(advisories))
