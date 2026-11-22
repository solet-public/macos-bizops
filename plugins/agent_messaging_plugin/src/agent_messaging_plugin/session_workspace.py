"""Working directory selection shared by session hosts."""

from __future__ import annotations

from pathlib import Path


def resolve_workspace(value: object, fallback: Path) -> Path:
    """Use the caller's existing directory, or the host default when omitted."""
    from .session_hosts import HostCannotSpawnError  # noqa: PLC0415 -- host registry imports the drivers

    directory = fallback if value is None or value == "" else Path(str(value))
    if not directory.is_dir():
        raise HostCannotSpawnError(f"spawn working directory is not an existing directory: {directory}")
    return directory.resolve()
