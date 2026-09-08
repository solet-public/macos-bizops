"""Hermetic per-lane Git worktree provisioning owned by the spawn path.

Workers never invoke this module against the source checkout.  The lifecycle spawn
and retirement paths call it on their behalf; Git-Controller remains the only
actor that stages, commits, merges, or otherwise mutates a lane worktree's Git
state.  Every teardown targets one derived path, never a glob or ``prune``.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_GIT_ENV_PREFIX = "GIT_"
_GIT_ENV_EXCEPTIONS = frozenset({"GIT_CONFIG_NOSYSTEM"})
_INTEGRATION_HEAD = "master"
_PROVISIONED_VENV_ENTRY = ".venv"
_REPORTED_PATH_LIMIT = 20

# Declared by name with a justification, never derived from ``.gitignore``: an
# ignore rule means "Git should not track this", which says nothing about whether
# the bytes are reproducible.  Every entry below is pure build or tool output.
_DISPOSABLE_UNTRACKED_DIRECTORIES = frozenset(
    {".ruff_cache", ".mypy_cache", ".pytest_cache", "__pycache__", "build"},
)
_DISPOSABLE_UNTRACKED_SUFFIXES = (".egg-info",)


class LaneWorktreeError(RuntimeError):
    """A requested worktree cannot be safely provisioned or removed."""


@dataclass(frozen=True, slots=True)
class LaneWorktree:
    """One deterministic lane branch and its isolated working directory."""

    repo_root: Path
    root: Path
    path: Path
    branch: str


@dataclass(frozen=True, slots=True)
class LaneWorktreeDisposability:
    """Whether removing one lane worktree can lose state that exists nowhere else."""

    disposable: bool
    offending_paths: tuple[str, ...]
    reason: str
    unlanded_commits: int


@dataclass(frozen=True, slots=True)
class DirtyStaleWorktreeSkippedWarning:
    """One retained stale worktree, safe to surface without blocking a spawn."""

    path: Path
    git_diagnostic: str
    code: Literal["dirty_stale_worktree_skipped"] = "dirty_stale_worktree_skipped"


@dataclass(frozen=True, slots=True)
class LaneWorktreeSweep:
    """What one opportunistic sweep removed, and what it left with a reason."""

    removed: tuple[Path, ...]
    skipped: tuple[DirtyStaleWorktreeSkippedWarning, ...]


GitRun = Callable[..., subprocess.CompletedProcess[str]]


def lane_worktree_root(repo_root: Path) -> Path:
    """Return the dedicated sibling directory used only by lane worktrees."""
    root = repo_root.resolve()
    return root.parent / f"{root.name}_lane_worktrees"


def lane_worktree_for(
    repo_root: Path,
    *,
    role_name: str,
    agent_instance_id: str,
) -> LaneWorktree:
    """Derive the one safe worktree path and branch for a spawned lane."""
    _require_component("role_name", role_name)
    _require_component("agent_instance_id", agent_instance_id)
    resolved_repo = repo_root.resolve()
    root = lane_worktree_root(resolved_repo)
    path = root / f"{role_name}--{agent_instance_id}"
    _assert_under_root(path, root)
    return LaneWorktree(
        repo_root=resolved_repo,
        root=root,
        path=path,
        branch=f"lane/{role_name}/{agent_instance_id}",
    )


def provision_lane_worktree(
    worktree: LaneWorktree,
    *,
    run: GitRun = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Create one branch/worktree and link its interpreter from the source tree.

    The caller must be the trusted spawn path.  The source checkout is never
    changed by a worker; this function delegates Git mutations only to that
    caller's process.  A partial provision is removed by its exact path.
    """
    _assert_worktree_shape(worktree)
    shared_venv = worktree.repo_root / ".venv"
    if not shared_venv.is_dir():
        raise LaneWorktreeError(f"shared venv is missing: {shared_venv}")
    if worktree.path.exists():
        raise LaneWorktreeError(f"lane worktree path already exists: {worktree.path}")
    worktree.root.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        _run_git(
            worktree.repo_root,
            ("worktree", "add", "-b", worktree.branch, str(worktree.path), "HEAD"),
            run=run,
            environment=environment,
        )
        created = True
        (worktree.path / ".venv").symlink_to(shared_venv, target_is_directory=True)
    except (OSError, LaneWorktreeError) as exc:
        if created:
            _remove_exact_worktree(worktree, run=run, environment=environment, suppress=True)
        raise LaneWorktreeError(f"lane worktree provisioning failed: {exc}") from exc
    return worktree.path


def lane_worktree_disposability(
    worktree: LaneWorktree,
    *,
    integration_head: str = _INTEGRATION_HEAD,
    run: GitRun = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> LaneWorktreeDisposability:
    """Decide, in Git's own diff semantics, whether removal can lose real work.

    Removal deletes the working directory and never the object database, and
    ``remove_lane_worktree`` preserves the lane branch forever, so committed work
    is never at risk.  Only uncommitted tracked changes and untracked residue can
    be lost, so those are the whole of what this adjudicates.

    The comparison is scoped to the paths the worktree itself has touched.  A
    lane stays checked out at its own base while the integration head moves, so
    an unscoped comparison would call almost every worktree not disposable for
    reasons belonging to other lanes.  Delegating each comparison to ``git diff``
    covers content, file mode, symlink target values, deletions, and type changes
    by construction rather than by enumerating them.
    """
    at_risk = _at_risk_paths(worktree, run=run, environment=environment)
    diverging = tuple(
        path
        for path in at_risk
        if not _git_diff_is_clean(
            worktree.path,
            ("diff", "--quiet", integration_head, "--", path),
            run=run,
            environment=environment,
        )
    )
    residue = tuple(
        path
        for path in _untracked_paths(worktree, run=run, environment=environment)
        if not _is_ignorable_untracked(path)
    )
    unlanded = _unlanded_commit_count(
        worktree, integration_head=integration_head, run=run, environment=environment,
    )
    offending = tuple(sorted({*diverging, *residue}))
    if offending:
        return LaneWorktreeDisposability(
            disposable=False,
            offending_paths=offending,
            reason=_not_disposable_reason(diverging, residue, integration_head),
            unlanded_commits=unlanded,
        )
    return LaneWorktreeDisposability(
        disposable=True,
        offending_paths=(),
        reason=(
            f"every uncommitted tracked change already matches {integration_head}, "
            "and every untracked path is declared build output"
        ),
        unlanded_commits=unlanded,
    )


def remove_lane_worktree(
    worktree: LaneWorktree,
    *,
    run: GitRun = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Remove one proved-disposable lane worktree; preserve its branch forever.

    The force Git needs to clear declared build output is earned by the
    predicate above and is unreachable without it, so no caller ever chooses it.
    """
    _assert_worktree_shape(worktree)
    if not worktree.path.exists():
        return
    verdict = lane_worktree_disposability(worktree, run=run, environment=environment)
    if not verdict.disposable:
        raise LaneWorktreeError(
            f"lane worktree holds state that exists nowhere else: {worktree.path}: "
            f"{verdict.reason}",
        )
    _remove_exact_worktree(
        worktree, run=run, environment=environment, suppress=False, force=True,
    )


def sweep_orphaned_lane_worktrees(
    repo_root: Path,
    *,
    active_paths: Iterable[Path],
    run: GitRun = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> LaneWorktreeSweep:
    """Remove registered, inactive, provably disposable lane worktrees only.

    Opportunistic tidying must never fail the operation it is tidying for, so a
    candidate that will not remove is collected in ``skipped`` with the reason
    and the loop continues.  Structural faults still raise, because a path
    escaping the dedicated root or Git being unusable means the sweep cannot do
    its job at all rather than that one orphan is stuck.

    This deliberately does not call ``git worktree prune``.  An existing
    directory that Git no longer lists is left untouched and reported by the
    caller instead of being guessed to be disposable.
    """
    root = lane_worktree_root(repo_root)
    if not root.exists():
        return LaneWorktreeSweep(removed=(), skipped=())
    active = {path.resolve() for path in active_paths}
    registered = _registered_worktree_paths(
        repo_root.resolve(), run=run, environment=environment,
    )
    removed: list[Path] = []
    skipped: list[DirtyStaleWorktreeSkippedWarning] = []
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        _assert_under_root(candidate, root)
        resolved = candidate.resolve()
        if resolved in active:
            continue
        if resolved not in registered:
            continue
        worktree = LaneWorktree(
            repo_root=repo_root.resolve(), root=root.resolve(), path=resolved, branch="",
        )
        try:
            remove_lane_worktree(worktree, run=run, environment=environment)
        except LaneWorktreeError as exc:
            skipped.append(
                DirtyStaleWorktreeSkippedWarning(
                    path=resolved,
                    git_diagnostic=str(exc),
                )
            )
            continue
        removed.append(resolved)
    return LaneWorktreeSweep(removed=tuple(removed), skipped=tuple(skipped))


def worktree_pythonpath(worktree_path: Path, existing: str = "") -> str:
    """Anchor imports in the lane tree before any inherited PYTHONPATH entries."""
    root = worktree_path.resolve()
    if not root.is_dir():
        raise LaneWorktreeError(f"worktree root is missing: {root}")
    return os.pathsep.join(part for part in (str(root), existing) if part)


def spawn_worktree_cwd(value: object, fallback: Path) -> Path:
    """Resolve a spawn's explicit lane root or retain the adapter's default."""
    raw = str(value or "").strip()
    root = Path(raw).resolve() if raw else fallback.resolve()
    if not root.is_dir() or (raw and not (root / ".git").exists()):
        raise LaneWorktreeError(f"spawn worktree is not a Git checkout: {root}")
    return root


def _at_risk_paths(
    worktree: LaneWorktree,
    *,
    run: GitRun,
    environment: Mapping[str, str] | None,
) -> tuple[str, ...]:
    """Paths whose uncommitted state removal would destroy, staged and unstaged.

    Comparing against ``HEAD`` drops committed work out of the set with no
    special-casing, because the branch that holds it is preserved forever.  This
    reading depends on the module invariant that workers never mutate Git inside
    a lane worktree, so ``HEAD`` is that lane's own provisioned branch tip.
    """
    result = _run_git(
        worktree.path, ("diff", "--name-only", "HEAD"), run=run, environment=environment,
    )
    return tuple(sorted({line for line in result.stdout.splitlines() if line}))


def _untracked_paths(
    worktree: LaneWorktree,
    *,
    run: GitRun,
    environment: Mapping[str, str] | None,
) -> tuple[str, ...]:
    """Every untracked entry, judged by the declared allowlist rather than ignores.

    ``--exclude-standard`` is deliberately absent: honouring ``.gitignore`` here
    would equate "Git should not track this" with "safe to delete", and an
    ignore file routinely hides locally-generated config that is neither.
    ``--directory`` collapses a wholly-untracked tree to its root so a build
    cache costs one entry instead of thousands.
    """
    result = _run_git(
        worktree.path,
        ("ls-files", "--others", "--directory"),
        run=run,
        environment=environment,
    )
    return tuple(sorted({line for line in result.stdout.splitlines() if line}))


def _is_ignorable_untracked(relative: str) -> bool:
    """Match one untracked path against the declared build-output allowlist."""
    parts = PurePosixPath(relative).parts
    if parts == (_PROVISIONED_VENV_ENTRY,):
        return True
    if any(part in _DISPOSABLE_UNTRACKED_DIRECTORIES for part in parts):
        return True
    return any(part.endswith(_DISPOSABLE_UNTRACKED_SUFFIXES) for part in parts)


def _unlanded_commit_count(
    worktree: LaneWorktree,
    *,
    integration_head: str,
    run: GitRun,
    environment: Mapping[str, str] | None,
) -> int:
    """Report commits the integration head lacks; advisory, never a disposal gate."""
    result = _run_git(
        worktree.path,
        ("rev-list", "--count", f"{integration_head}..HEAD"),
        run=run,
        environment=environment,
    )
    return int(result.stdout.strip() or "0")


def _not_disposable_reason(
    diverging: tuple[str, ...], residue: tuple[str, ...], integration_head: str,
) -> str:
    """Name the paths that refused disposal; a generic refusal teaches nothing."""
    clauses: list[str] = []
    if diverging:
        clauses.append(
            f"uncommitted tracked changes absent from {integration_head}: "
            f"{_render_paths(diverging)}",
        )
    if residue:
        clauses.append(
            f"untracked paths outside the declared build-output allowlist: "
            f"{_render_paths(residue)}",
        )
    return "; ".join(clauses)


def _render_paths(paths: tuple[str, ...]) -> str:
    """List the offending paths under a bound, keeping the elided count honest."""
    shown = ", ".join(paths[:_REPORTED_PATH_LIMIT])
    remainder = len(paths) - _REPORTED_PATH_LIMIT
    return shown if remainder <= 0 else f"{shown} (+{remainder} more)"


def _git_diff_is_clean(
    repo_root: Path,
    arguments: tuple[str, ...],
    *,
    run: GitRun,
    environment: Mapping[str, str] | None,
) -> bool:
    """Read a ``--quiet`` diff: 0 is no difference, 1 is one, anything else a fault."""
    result = run(
        ("git", "-C", str(repo_root), *arguments),
        check=False,
        capture_output=True,
        text=True,
        env=_git_environment(environment),
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = (result.stderr or result.stdout).strip()
    raise LaneWorktreeError(f"git {' '.join(arguments)} failed: {detail}")


def _require_component(label: str, value: str) -> None:
    if not _SAFE_COMPONENT.fullmatch(value):
        raise LaneWorktreeError(f"{label} must match {_SAFE_COMPONENT.pattern}: {value!r}")


def _assert_worktree_shape(worktree: LaneWorktree) -> None:
    if not (worktree.repo_root / ".git").exists():
        raise LaneWorktreeError(f"repo root is not a Git checkout: {worktree.repo_root}")
    _assert_under_root(worktree.path, worktree.root)


def _assert_under_root(candidate: Path, root: Path) -> None:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise LaneWorktreeError(f"worktree path escapes dedicated root: {candidate}") from exc


def _run_git(
    repo_root: Path,
    arguments: tuple[str, ...],
    *,
    run: GitRun,
    environment: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    result = run(
        ("git", "-C", str(repo_root), *arguments),
        check=False,
        capture_output=True,
        text=True,
        env=_git_environment(environment),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LaneWorktreeError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def _git_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    """Use supplied hermetic test environment without inherited Git redirection."""
    source = os.environ if environment is None else environment
    return {
        key: value
        for key, value in source.items()
        if not key.startswith(_GIT_ENV_PREFIX) or key in _GIT_ENV_EXCEPTIONS
    }


def _remove_exact_worktree(
    worktree: LaneWorktree,
    *,
    run: GitRun,
    environment: Mapping[str, str] | None,
    suppress: bool,
    force: bool = False,
) -> None:
    """Remove one derived path; ``force`` clears residue a predicate already cleared."""
    if not worktree.path.exists():
        return
    flags = ("--force",) if force else ()
    try:
        _remove_shared_venv_link(worktree)
        _run_git(
            worktree.repo_root,
            ("worktree", "remove", *flags, str(worktree.path)),
            run=run,
            environment=environment,
        )
    except LaneWorktreeError:
        if not suppress:
            raise


def _remove_shared_venv_link(worktree: LaneWorktree) -> None:
    """Discard only the provisioning-owned venv link before clean teardown."""
    link = worktree.path / ".venv"
    if not link.exists() and not link.is_symlink():
        return
    shared_venv = (worktree.repo_root / ".venv").resolve()
    if not link.is_symlink() or link.resolve() != shared_venv:
        raise LaneWorktreeError(f"refusing to remove non-provisioned venv entry: {link}")
    link.unlink()


def _registered_worktree_paths(
    repo_root: Path,
    *,
    run: GitRun,
    environment: Mapping[str, str] | None,
) -> set[Path]:
    result = _run_git(
        repo_root, ("worktree", "list", "--porcelain"), run=run, environment=environment,
    )
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    }
