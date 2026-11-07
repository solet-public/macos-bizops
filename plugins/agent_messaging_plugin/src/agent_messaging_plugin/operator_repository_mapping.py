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


_SECOND_REPOSITORY = "project-solet"
_SECOND_CONTROLLER = "Project-Solet-Git-Controller"
type RepositoryRoots = tuple[tuple[Path, ...], tuple[Path, ...]]


def _family_for(target: Path, families: RepositoryRoots) -> int | None:
    """Match a path against roots without changing their spelling."""
    return next(
        (index for index, roots in enumerate(families) if any(
            target.is_relative_to(root) for root in roots
        )), None,
    )


def _cross_controller_overlap(families: RepositoryRoots) -> bool:
    """An ambiguous canonical root must never grant either controller."""
    return any(
        first.is_relative_to(second) or second.is_relative_to(first)
        for first in families[0]
        for second in families[1]
    )


def _support_checkout() -> Path:
    """Find the checkout shared by this source tree and its linked worktrees."""
    source_root = Path(__file__).resolve().parents[4]
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("operator source checkout could not be inspected") from exc
    if result.returncode or not result.stdout.strip():
        raise ValueError("operator source is not a Git checkout")
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = source_root / common_dir
    common_dir = common_dir.resolve()
    if common_dir.name != ".git":
        raise ValueError("operator source has no standard shared Git directory")
    return common_dir.parent


def resolve_repository_target(cwd: Path) -> OperatorRepositoryTarget:
    """Resolve the two registered checkout families; refuse every other path."""
    support_root = _support_checkout()
    workspace = support_root.parent
    support_name = support_root.name
    if support_name == _SECOND_REPOSITORY:
        raise ValueError("operator repository bindings overlap")
    bridge = support_root / ".venv" / "bin" / "solet-bridge"
    expanded_cwd = cwd.expanduser()
    target = expanded_cwd.resolve()
    lexical_target = Path(os.path.abspath(expanded_cwd))
    normalized_target = lexical_target.resolve()
    lexical_roots = (
        (support_root, workspace / f"{support_name}_lane_worktrees"),
        (workspace / _SECOND_REPOSITORY, workspace / f"{_SECOND_REPOSITORY}_lane_worktrees"),
    )
    canonical_roots: RepositoryRoots = (
        tuple(root.resolve() for root in lexical_roots[0]),
        tuple(root.resolve() for root in lexical_roots[1]),
    )
    if _cross_controller_overlap(canonical_roots):
        raise ValueError("operator repository bindings overlap")
    classifications = (
        _family_for(lexical_target, lexical_roots),
        _family_for(normalized_target, lexical_roots),
        _family_for(target, lexical_roots),
        _family_for(target, canonical_roots),
    )
    if classifications[0] is None or len(set(classifications)) != 1:
        raise ValueError(f"operator repository authority disagrees for {lexical_target}")
    bindings = (
        OperatorRepositoryTarget(support_name, bridge, "Git-Controller"),
        OperatorRepositoryTarget(support_name, bridge, _SECOND_CONTROLLER),
    )
    return bindings[classifications[0]]
