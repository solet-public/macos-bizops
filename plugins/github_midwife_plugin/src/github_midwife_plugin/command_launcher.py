"""SEED — install the per-homunculus command launcher at birth.

A genesis spine phase that puts a ``<name>`` command on the operator's PATH,
pointing at the newborn's own ``homunculus`` console script. This is the
no-MCP-first primary interface: after birth, ``<name> search ...`` and
``<name> call ...`` drive the homunculus over its localhost bridge with NO
MCP required.

Bare symlink by design: the ``homunculus`` CLI derives its identity from its OWN
install location (``local_cli.client.resolve_homunculus_name`` walks to the
clone root), so a symlink from anywhere on PATH resolves into THIS newborn's
venv and pins THIS newborn — reaching no sibling. UNCONDITIONAL (every profile
ships ``agent_messaging_plugin``, so every newborn has the console script).
Idempotent: a correct existing symlink is a no-op; a stale/wrong symlink is
repointed; a NON-symlink already at the path is a fail-loud refusal (never
clobber a real operator file).

Ensuring ``bin_dir`` is ON the operator's PATH is the hydration shell step's job,
not this phase's — this phase only creates the launcher in a well-known dir.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .constants import NAME_PATTERN, is_valid_homunculus_name

# The generic console-script name the launcher points at (the
# ``[project.scripts]`` entry of ``agent_messaging_plugin``). One generic name
# on disk; the per-homunculus name is the SYMLINK, resolved to identity by
# install location — so no shipped surface carries a specific homunculus name.
CONSOLE_SCRIPT_NAME = "homunculus"

# Default operator-PATH bin dir for the launcher: user-writable, no sudo. The
# hydration shell step ensures it is on PATH.
DEFAULT_BIN_DIR = Path.home() / ".local" / "bin"


class CommandLauncherError(RuntimeError):
    """The per-homunculus command launcher could not be installed."""


@dataclass(frozen=True, slots=True)
class CommandLauncherResult:
    status: str  # "installed" | "repointed" | "already_installed"
    reason: str
    launcher_path: str
    target: str


def install_command_launcher_at_birth(
    *,
    name: str,
    clone_root: Path,
    bin_dir: Path = DEFAULT_BIN_DIR,
) -> CommandLauncherResult:
    """Symlink ``<bin_dir>/<name>`` -> the newborn's ``homunculus`` console script.

    Raises :class:`CommandLauncherError` when the console script is missing (the
    venv must be provisioned + the plugin installed first) or when a NON-symlink
    file already occupies the launcher path (never clobber an operator file).
    """
    # Defense in depth: genesis validates the name first, but the launcher path
    # is `bin_dir / name`, so a bad name could escape bin_dir — refuse one here.
    if not is_valid_homunculus_name(name):
        raise CommandLauncherError(
            f"refusing to install a launcher for invalid homunculus name {name!r} "
            f"(must match {NAME_PATTERN.pattern})."
        )
    target = clone_root / ".venv" / "bin" / CONSOLE_SCRIPT_NAME
    if not target.is_file():
        raise CommandLauncherError(
            f"console script missing at {target} — the venv must be provisioned "
            f"and {CONSOLE_SCRIPT_NAME}'s plugin installed before the launcher."
        )
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / name

    if launcher.is_symlink():
        if launcher.readlink() == target:
            return CommandLauncherResult(
                status="already_installed",
                reason="launcher already points at this newborn's console script",
                launcher_path=str(launcher), target=str(target),
            )
        launcher.unlink()
        launcher.symlink_to(target)
        return CommandLauncherResult(
            status="repointed",
            reason="launcher repointed to this newborn's console script",
            launcher_path=str(launcher), target=str(target),
        )
    if launcher.exists():
        raise CommandLauncherError(
            f"{launcher} already exists and is not a symlink — refusing to clobber "
            "an operator file; move it aside and re-run."
        )
    launcher.symlink_to(target)
    return CommandLauncherResult(
        status="installed",
        reason="per-homunculus command launcher installed on PATH",
        launcher_path=str(launcher), target=str(target),
    )


__all__ = [
    "CONSOLE_SCRIPT_NAME",
    "DEFAULT_BIN_DIR",
    "CommandLauncherError",
    "CommandLauncherResult",
    "install_command_launcher_at_birth",
]
