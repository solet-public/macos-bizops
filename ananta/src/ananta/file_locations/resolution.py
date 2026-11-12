"""Resolve injected location inputs without database or process-cwd dependencies."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from .advisories import directory_advisory


class Rung(StrEnum):
    EXPLICIT = "explicit"
    PROJECT = "project"
    LAUNCH_DIRECTORY = "launch_directory"
    SOLET_HOME = "solet_home"


class Purpose(StrEnum):
    FILE = "file"
    EXPORT = "export"


@dataclass(frozen=True, slots=True)
class PathAddress:
    name: str
    directory: Path
    projects: frozenset[str]
    file_kinds: frozenset[str]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SoletTree:
    repo_root: Path
    app_home: Path


def solet_tree_from_app_home(app_home: Path) -> SoletTree:
    """Use the canonical <repo_root>/profile layout."""
    return SoletTree(app_home.parent, app_home)


@dataclass(frozen=True, slots=True)
class LocationRequest:
    explicit_path: str | None
    project: str | None
    file_kind: str | None
    purpose: Purpose
    launch_directory: str | None


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    directory: Path
    rung: Rung
    address_name: str | None
    advisories: tuple[str, ...]


def overlaps_solet_tree(path: Path, tree: SoletTree) -> bool:
    """Check both containment directions after resolving symlinks."""
    candidate = os.path.realpath(path)
    if sys.platform in ("darwin", "win32"):
        candidate = candidate.casefold()
    for own in (tree.repo_root, tree.app_home):
        own_real = os.path.realpath(own)
        if sys.platform in ("darwin", "win32"):
            own_real = own_real.casefold()
        try:
            shared = os.path.commonpath((candidate, own_real))
        except ValueError:  # Different drives have no containment relationship.
            continue
        if shared in (candidate, own_real):
            return True
    return False


def _directory(raw: str, tree: SoletTree, label: str, advisories: list[str]) -> Path | None:
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        advisories.append(f"{label} is not an absolute directory; trying the next location.")
    elif overlaps_solet_tree(path, tree):
        advisories.append(f"{label} overlaps the solet's own tree; launch from a project folder outside it. Trying the next location.")
    else:
        advisory = directory_advisory(path, sys.platform, sys.executable)
        if advisory is not None:
            advisories.append(f"{label}: {advisory}")
            return None
        return path.resolve()
    return None


def _project_matches(request: LocationRequest, addresses: Sequence[PathAddress]) -> list[PathAddress]:
    project = (request.project or "").strip().lower()
    kind = (request.file_kind or "").strip().lower()
    matches = [address for address in addresses if _matches_project(address, project, kind)]
    return sorted(matches, key=lambda address: (kind in address.file_kinds, address.updated_at, address.name), reverse=True)


def _matches_project(address: PathAddress, project: str, kind: str) -> bool:
    if project:
        return project in address.projects
    return bool(kind) and not address.projects and kind in address.file_kinds


def _home_location(request: LocationRequest, addresses: Sequence[PathAddress], tree: SoletTree, solet_home: Path, advisories: list[str]) -> ResolvedLocation:
    home_address = next((address for address in addresses if address.name == "solet_home"), None)
    if home_address is not None and not overlaps_solet_tree(home_address.directory, tree):
        home = home_address.directory
        name = home_address.name
    else:
        home = solet_home
        name = None
        advisories.append("solet_home address is missing or overlaps the solet tree; using the computed Documents default.")
    if request.purpose == Purpose.EXPORT:
        home = home / "Exports.nosync"
    advisory = directory_advisory(home, sys.platform, sys.executable, terminal=True)
    if advisory is not None:
        advisories.append(advisory)
    return ResolvedLocation(home, Rung.SOLET_HOME, name, tuple(advisories))


def resolve_location(
    request: LocationRequest,
    addresses: Sequence[PathAddress],
    tree: SoletTree,
    solet_home: Path,
) -> ResolvedLocation:
    """Try explicit, project and launch folders, then always yield a home path."""
    advisories: list[str] = []
    if request.explicit_path:
        directory = _directory(request.explicit_path, tree, "Explicit path", advisories)
        if directory is not None:
            return ResolvedLocation(directory, Rung.EXPLICIT, None, tuple(advisories))
    for address in _project_matches(request, addresses):
        directory = _directory(str(address.directory), tree, f"Project address {address.name}", advisories)
        if directory is not None:
            return ResolvedLocation(directory, Rung.PROJECT, address.name, tuple(advisories))
    if request.launch_directory:
        directory = _directory(request.launch_directory, tree, "Session launch directory", advisories)
        if directory is not None:
            return ResolvedLocation(directory, Rung.LAUNCH_DIRECTORY, None, tuple(advisories))
    else:
        advisories.append("Session launch directory is missing. Refresh the launcher in hydration step 2, then launch from your project folder; using solet_home.")
    return _home_location(request, addresses, tree, solet_home, advisories)
