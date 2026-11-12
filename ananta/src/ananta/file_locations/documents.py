"""Per-user Documents resolution; macOS is the proven A1 platform."""

from __future__ import annotations

import ctypes
import os
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .advisories import onedrive_advisory
from .containment import Located
from .resolution import SoletTree, overlaps_solet_tree


@dataclass(frozen=True, slots=True)
class DocumentsFolder:
    path: Path
    source: str
    proven: bool
    advisories: tuple[str, ...] = ()


def _windows_documents() -> Path:
    """Read FOLDERID_Documents, including any user/OneDrive redirection."""
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7").bytes_le)
    result = ctypes.c_void_p()
    shell.SHGetKnownFolderPath.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole.CoTaskMemFree.restype = None
    try:
        status = shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(result))
        if status != 0 or result.value is None:
            raise OSError(f"SHGetKnownFolderPath(FOLDERID_Documents) failed: HRESULT {status:#x}")
        return Path(ctypes.wstring_at(result.value))
    finally:
        if result.value is not None:
            ole.CoTaskMemFree(result)


def _xdg_path(value: str, home: Path) -> Path | None:
    expanded = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if expanded == "~" or expanded.startswith("~/"):
        expanded = str(home) + expanded[1:]
    path = Path(expanded)
    return path if expanded and path.is_absolute() else None


def _configured_documents(config: Path, home: Path) -> Path | None:
    for line in config.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "XDG_DOCUMENTS_DIR":
            words = shlex.split(value, comments=True)
            return _xdg_path(words[0], home) if len(words) == 1 else None
    return None


def _linux_documents(environ: Mapping[str, str], home: Path) -> DocumentsFolder:
    advisories: list[str] = []
    raw = environ.get("XDG_DOCUMENTS_DIR")
    if raw:
        directory = _xdg_path(raw, home)
        if directory is not None:
            return DocumentsFolder(directory, "XDG_DOCUMENTS_DIR environment", False)
        advisories.append("XDG_DOCUMENTS_DIR is not absolute; reading user-dirs.dirs.")
    config = Path(environ.get("XDG_CONFIG_HOME") or str(home / ".config")) / "user-dirs.dirs"
    try:
        directory = _configured_documents(config, home)
        if directory is not None:
            return DocumentsFolder(directory, str(config), False, tuple(advisories))
        advisories.append(f"Missing or invalid Documents entry in {config}; using ~/Documents.")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        advisories.append(f"Could not read Documents from {config}: {error}; using ~/Documents.")
    return DocumentsFolder(home / "Documents", "~/Documents fallback", False, tuple(advisories))


def documents_folder(platform: str, environ: Mapping[str, str], home: Path) -> DocumentsFolder:
    """Resolve the per-user folder; only macOS is proven on the guest ladder."""
    if platform == "darwin":
        return DocumentsFolder(home / "Documents", "macOS Documents", True)
    if platform == "linux":
        return _linux_documents(environ, home)
    if platform == "win32":
        try:
            folder = _windows_documents()
        except (OSError, AttributeError) as error:
            return DocumentsFolder(home / "Documents", "Windows Documents fallback", False, (f"Windows known folder unavailable: {error}; using ~/Documents.",))
        advisory = onedrive_advisory(folder)
        return DocumentsFolder(folder, "Windows Documents known folder", False, (advisory,) if advisory else ())
    return DocumentsFolder(home / "Documents", "unproven ~/Documents fallback", False)


def _terminal_home(tree: SoletTree) -> Path:
    """Find an admissible sibling of the outermost root, avoiding file collisions."""
    repo = tree.repo_root.resolve()
    app = tree.app_home.resolve()
    repo_key, app_key = str(repo), str(app)
    if sys.platform in ("darwin", "win32"):
        repo_key, app_key = repo_key.casefold(), app_key.casefold()
    try:
        outer = app if os.path.commonpath((repo_key, app_key)) == app_key else repo
    except ValueError:
        outer = repo
    number = 1
    while True:
        suffix = "" if number == 1 else f" {number}"
        candidate = outer.parent / f"{outer.name} files{suffix}"
        if not overlaps_solet_tree(candidate, tree) and (not candidate.exists() or candidate.is_dir()):
            return candidate
        number += 1


def default_solet_home(solet_name: str, documents: DocumentsFolder, tree: SoletTree, app_support: Path) -> Located[Path]:
    """Walk Documents, the supplied application-support location and a safe sibling.

    app_support is the full platform candidate, including the solet name and
    Files directory, so callers can inject it without platform dependencies.
    """
    advisories: list[str] = list(documents.advisories)
    if not documents.proven:
        advisories.append(f"Documents location is unproven: {documents.source}.")
    for candidate in (documents.path / solet_name, app_support):
        if overlaps_solet_tree(candidate, tree) or (candidate.exists() and not candidate.is_dir()):
            advisories.append(f"Skipping unavailable or tree-overlapping solet_home candidate {candidate}.")
            continue
        advisories.append(f"Using solet_home {candidate}; say 'keep your files in <folder>' to change it.")
        return Located(candidate, tuple(advisories))
    terminal = _terminal_home(tree)
    advisories.append(f"Using solet_home {terminal}; say 'keep your files in <folder>' to change it.")
    return Located(terminal, tuple(advisories))


def exports_folder(solet_home: Path) -> Path:
    """Keep large exports in the iCloud-excluded child folder."""
    return solet_home / "Exports.nosync"
