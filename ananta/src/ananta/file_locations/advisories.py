"""Location advisories: an operating-system denial never ends resolution."""

from __future__ import annotations

import errno
import tempfile
from pathlib import Path


def protected_folder_advisory(path: Path, error: OSError, platform: str, launching_binary: str) -> str | None:
    """Name the macOS privacy grant for a denied protected user folder."""
    if platform != "darwin" or error.errno not in (errno.EPERM, errno.EACCES):
        return None
    for folder in ("Documents", "Desktop", "Downloads"):
        protected = Path.home() / folder
        if path == protected or protected in path.parents:
            return f"Cannot write to {path}: macOS has not given {launching_binary} access to your {folder} folder. Open System Settings → Privacy & Security → Files and Folders (or Full Disk Access) and allow {launching_binary}. Choose another saved location until access is granted."
    return None


def directory_advisory(path: Path, platform: str, launching_binary: str, *, terminal: bool = False) -> str | None:
    """Probe a candidate in the caller's process and return its unusable reason."""
    try:
        if not path.is_dir():
            if terminal:  # The caller creates the terminal home after resolution.
                return None
            return f"Directory is missing or unavailable: {path}; trying the next location."
        with tempfile.TemporaryFile(dir=path) as probe:
            probe.write(b"solet location write check")
            probe.flush()
    except OSError as error:
        action = "grant access or choose another location" if terminal else "trying the next location"
        return protected_folder_advisory(path, error, platform, launching_binary) or f"Cannot write to {path}: {error}; {action}."
    return None


def onedrive_advisory(path: Path) -> str | None:
    """OneDrive has no equivalent of iCloud's .nosync folder convention."""
    if any(part.casefold().startswith("onedrive") for part in path.parts):
        return f"Documents at {path} is in OneDrive; Exports.nosync does not exclude OneDrive sync. Choose another saved location for large exports if you do not want them copied."
    return None
