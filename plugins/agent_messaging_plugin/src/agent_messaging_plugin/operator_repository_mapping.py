"""Resolve operator repository authority from the origin development checkout.

This module imports without origin hooks or an installed bridge. Resolution is
only meaningful in the development checkout that owns the operator launcher.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OperatorRepositoryTarget:
    """The support solet, bridge, and Git authority for a checkout."""

    solet: str
    bridge_cli: Path
    git_controller_name: str


def _git(directory: Path, *args: str) -> str:
    """Read the checkout at directory independently of the launching Git process."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            env=env,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"operator checkout could not be inspected: {directory}") from exc
    return os.fsdecode(result.stdout)


def _common_dir(directory: Path) -> Path:
    path = _git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(path.removesuffix("\n")).resolve()


def _support_checkout() -> Path:
    """Find the support checkout from this module, including in a linked checkout."""
    common = _common_dir(Path(__file__).resolve().parents[4])
    if common.name != ".git":
        raise ValueError("operator source has no standard shared Git directory")
    return common.parent


def resolve_repository_target(cwd: Path) -> OperatorRepositoryTarget:
    """Grant operator authority to the support checkout and its registered working trees."""
    support = _support_checkout()
    supplied = cwd.expanduser().absolute()
    # Inspect original components too: normalizing '..' can hide a symlink crossing.
    if any(part.is_symlink() for part in (supplied, *supplied.parents)):
        raise ValueError(f"operator repository authority refuses {supplied}")
    target = Path(os.path.abspath(supplied))
    if not supplied.is_dir() or target != supplied.resolve() or _common_dir(target) != support / ".git":
        raise ValueError(f"operator repository authority refuses {supplied}")
    top = _git(target, "rev-parse", "--show-toplevel").removesuffix("\n")
    trees = _git(support, "worktree", "list", "--porcelain", "-z").split("\0")
    if not target.is_relative_to(top) or f"worktree {top}" not in trees:
        raise ValueError(f"operator repository authority refuses {supplied}")
    return OperatorRepositoryTarget(support.name, support / ".venv/bin/solet-bridge", "Git-Controller")
