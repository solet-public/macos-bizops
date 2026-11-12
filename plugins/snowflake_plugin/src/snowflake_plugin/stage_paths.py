"""Absolute local-path containment for both directions of stage transfers."""

from __future__ import annotations

import glob
import os
from collections.abc import Callable
from pathlib import Path

from ananta.file_locations.containment import PathRefusedError


def contained(path: str, gate: Callable[[str], str]) -> Path:
    """Delegate to the plugin's single assert_path_allowed binding."""
    return Path(gate(path))


def upload_paths(path: str, roots: Callable[[str], str]) -> list[Path]:
    """Expand vendor-supported wildcards, then admit every resolved file before I/O."""
    contained(path, roots)
    matches = sorted(glob.glob(path)) if glob.has_magic(path) else [path]
    if not matches:
        raise ValueError("local_path matched no files")
    files = [contained(match, roots) for match in matches]
    if not all(file.is_file() for file in files):
        raise ValueError("PUT local_path must name regular files")
    # Realpaths with glob metacharacters would be re-expanded by the connector.
    if any(glob.has_magic(str(file)) for file in files):
        raise PathRefusedError("resolved PUT filenames must not contain wildcard characters")
    return files


def download_directory(path: str, roots: Callable[[str], str]) -> Path:
    directory = contained(path, roots)
    if not directory.is_dir():
        raise ValueError("GET local_path must be an existing directory")
    return directory


def publish_downloads(scratch: Path, destination: Path) -> list[str]:
    """Replace entries atomically: an existing symlink is replaced, never followed."""
    files = sorted(scratch.iterdir())
    if any(file.is_symlink() or not file.is_file() for file in files):
        raise PathRefusedError("download contained a non-regular file")
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for file in files:
            os.replace(file, file.name, dst_dir_fd=descriptor)
    finally:
        os.close(descriptor)
    return [str(destination / file.name) for file in files]
