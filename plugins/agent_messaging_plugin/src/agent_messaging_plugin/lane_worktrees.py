"""Hermetic per-lane Git worktree provisioning owned by the spawn path.

Workers never invoke this module against the source checkout.  The lifecycle spawn
and retirement paths call it on their behalf; Git-Controller remains the only
actor that stages, commits, merges, or otherwise mutates a lane worktree's Git
state.  Every teardown targets one derived path, never a glob or ``prune``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.services.state_service.bounded_read import PagedReadError

from .lane_worktree_evidence import (
    CUSTODY_BUILD_OUTPUT,
    CUSTODY_CURRENT_MASTER,
    CUSTODY_EMPTY_SCAFFOLD,
    CUSTODY_FROZEN_CANDIDATE,
    CUSTODY_LANDED_HISTORY,
    CUSTODY_REGISTERED_DOCUMENT,
    CUSTODY_VERIFIED_PACKET,
    CandidateCustody,
    FinalEntry,
    LocalEntry,
    PacketCustody,
    RegisterCustody,
    RetirementCustodyContext,
    candidate_packet,
    read_entry,
)
from .schema import (
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
)

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_GIT_ENV_PREFIX = "GIT_"
_GIT_ENV_EXCEPTIONS = frozenset({"GIT_CONFIG_NOSYSTEM"})
_INTEGRATION_HEAD = "master"
_LOGGER = logging.getLogger(__name__)
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


class LaneWorktreeRepoRootError(LaneWorktreeError):
    """A supplied source checkout cannot safely host a lane worktree."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_lane_repo_root(repository_root: str, app_home: str) -> Path:
    """Resolve an explicit checkout root, or the serving checkout when absent."""
    if repository_root.strip() and not Path(repository_root).is_absolute():
        raise LaneWorktreeRepoRootError(
            "lane_worktree_repo_override_invalid",
            "lane worktree repository_root must be an absolute checkout path",
        )
    candidate = Path(repository_root) if repository_root.strip() else Path(app_home).resolve().parent
    source = "repository_root override" if repository_root.strip() else "APP_HOME"
    try:
        resolved = candidate.expanduser().resolve(strict=True)
    except OSError as exc:
        raise LaneWorktreeRepoRootError(
            "lane_worktree_repo_missing",
            f"lane worktree provisioning needs a Git checkout; {source} resolved {candidate}",
        ) from exc
    if not (resolved / ".git").exists():
        raise LaneWorktreeRepoRootError(
            "lane_worktree_repo_missing",
            f"lane worktree provisioning needs a Git checkout; {source} resolved {resolved}",
        )
    return resolved


@dataclass(frozen=True, slots=True)
class LaneWorktree:
    """One deterministic lane branch and its isolated working directory."""

    repo_root: Path
    root: Path
    path: Path
    branch: str


@dataclass(frozen=True, slots=True)
class LaneWorktreeDisposability:
    """Whether removing one lane worktree can lose state that exists nowhere else.

    ``custody`` names, per class, the paths removal may discard because an exact
    copy is recoverable or the bytes are reproducible.  ``advisories`` records
    register evidence that could not be read or verified for this call.
    """

    disposable: bool
    offending_paths: tuple[str, ...]
    reason: str
    unlanded_commits: int
    custody: tuple[tuple[str, tuple[str, ...]], ...] = ()
    advisories: tuple[str, ...] = ()


class LaneWorktreeNotDisposableError(LaneWorktreeError):
    """Removal refused because the worktree holds bytes with no recoverable copy."""

    def __init__(self, path: Path, verdict: LaneWorktreeDisposability) -> None:
        super().__init__(f"lane worktree holds state that exists nowhere else: {path}: {verdict.reason}")
        self.verdict = verdict


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


@dataclass(frozen=True, slots=True)
class ActiveLaneWorktreeInventory:
    """Complete active-path inventory plus whether automatic cleanup is safe."""

    paths: tuple[Path, ...]
    terminal_paths: tuple[Path, ...]
    cleanup_safe: bool
    incomplete_rows: tuple[str, ...]


def active_lane_worktree_inventory(
    state: StateManagementInterface,
    repo_root: Path,
) -> ActiveLaneWorktreeInventory:
    """Derive protected and terminal paths from complete managed-session evidence."""
    from .session_lifecycle_store import list_managed_sessions

    active_states = [
        LIFECYCLE_SPAWNING,
        LIFECYCLE_LIVE,
        LIFECYCLE_IDLE,
        LIFECYCLE_OVERDUE,
        LIFECYCLE_PARKED,
    ]
    try:
        rows = list_managed_sessions(
            state,
            {"lifecycle_state": [*active_states, LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED]},
        )
    except PagedReadError as exc:
        return ActiveLaneWorktreeInventory(
            paths=(),
            terminal_paths=(),
            cleanup_safe=False,
            incomplete_rows=(f"managed_session inventory unreadable: {exc}",),
        )
    paths: list[Path] = []
    terminal_paths: list[Path] = []
    incomplete_rows: list[str] = []
    for row in rows:
        active, row_paths, error = _inventory_row(row, repo_root, active_states)
        if error:
            if active:
                incomplete_rows.append(error)
            continue
        (paths if active else terminal_paths).extend(row_paths)
    active_path_set = set(paths)
    return ActiveLaneWorktreeInventory(
        paths=tuple(paths),
        terminal_paths=tuple(path for path in terminal_paths if path not in active_path_set),
        cleanup_safe=not incomplete_rows,
        incomplete_rows=tuple(incomplete_rows),
    )


def _inventory_row(row: dict[str, Any], repo_root: Path, active_states: list[str]) -> tuple[bool, tuple[Path, ...], str]:
    """Return one row's protected paths or a durable-identity error."""
    if str(row.get("provisioning_mode") or "") in {
        "operator_existing_checkout",
        "synthetic_no_worktree",
    }:
        return False, (), ""
    active = str(row.get("lifecycle_state") or "") in active_states
    row_id = str(row.get("id") or row.get("agent_instance_id") or "<unknown>")
    try:
        if _inventory_row_root(row, repo_root) is None:
            return active, (), ""
        return active, _inventory_row_paths(row, repo_root), ""
    except ValueError as exc:
        return active, (), f"{row_id}: {exc}"
    except LaneWorktreeError as exc:
        return active, (), f"{row_id}: lane identity invalid: {exc}"


def _inventory_row_root(row: dict[str, Any], repo_root: Path) -> Path | None:
    """Validate a worktree row's durable root, or ignore another repository."""
    if str(row.get("provisioning_mode") or "") != "worktree":
        raise ValueError("provisioning_mode invalid")
    recorded_root = str(row.get("lane_repo_root") or "")
    if not recorded_root:
        raise ValueError("lane_repo_root missing")
    if not Path(recorded_root).is_absolute():
        raise ValueError("lane_repo_root not absolute")
    try:
        row_root = Path(recorded_root).resolve(strict=True)
    except OSError as exc:
        raise ValueError("lane_repo_root invalid") from exc
    return row_root if row_root == repo_root.resolve() else None


def _inventory_row_paths(row: dict[str, Any], repo_root: Path) -> tuple[Path, ...]:
    """Reconstruct every durable role/local path associated with one row."""
    role_name = str(row.get("role_name") or "")
    local_name = str(row.get("local_name") or "")
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id or not (role_name or local_name):
        raise ValueError("lane identity incomplete")
    return tuple(lane_worktree_for(repo_root, role_name=name, agent_instance_id=agent_instance_id).path for name in dict.fromkeys((role_name, local_name)) if name)


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
    """Create one branch/worktree from the integration head and link its venv.

    The caller must be the trusted spawn path.  The source checkout is never
    changed by a worker; this function delegates Git mutations only to that
    caller's process.  If the local integration branch is unavailable, it logs
    the diagnostic and provisions from HEAD.  A partial provision is removed
    by its exact path.
    """
    _assert_worktree_shape(worktree)
    shared_venv = worktree.repo_root / ".venv"
    if not shared_venv.is_dir():
        raise LaneWorktreeError(f"shared venv is missing: {shared_venv}")
    if worktree.path.exists():
        raise LaneWorktreeError(f"lane worktree path already exists: {worktree.path}")
    worktree.root.mkdir(parents=True, exist_ok=True)
    integration_ref = f"refs/heads/{_INTEGRATION_HEAD}"
    start_point = integration_ref
    try:
        _run_git(
            worktree.repo_root,
            ("rev-parse", "--verify", f"{integration_ref}^{{commit}}"),
            run=run,
            environment=environment,
        )
    except LaneWorktreeError as exc:
        start_point = "HEAD"
        _LOGGER.warning(
            "local integration ref %s unavailable for repository %s; provisioning from HEAD: %s",
            integration_ref,
            worktree.repo_root,
            exc,
        )
    created = False
    try:
        _run_git(
            worktree.repo_root,
            (
                "worktree",
                "add",
                "-b",
                worktree.branch,
                str(worktree.path),
                start_point,
            ),
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
    custody: RetirementCustodyContext | None = None,
) -> LaneWorktreeDisposability:
    """Decide whether removal can lose bytes that have no exact recoverable copy.

    Removal deletes the working directory and never the object database, and
    ``remove_lane_worktree`` preserves the lane branch forever, so committed work
    is never at risk.  Only uncommitted tracked changes and untracked residue can
    be lost, so those are the whole of what this adjudicates.

    The comparison is scoped to the paths the worktree itself has touched.  A
    lane stays checked out at its own base while the integration head moves, so
    an unscoped comparison would call almost every worktree not disposable for
    reasons belonging to other lanes.  Each such path, and each untracked leaf,
    needs an exact copy, matched per path and never per tree: its raw bytes, Git
    mode and kind, or its absence, in the integration head's tree or, only when
    a retiring session supplies ``custody``, in a landing commit reachable from
    that head, a frozen candidate of its own Unit or a registered document of
    that Unit.  Declared build output and empty directories hold no bytes.  The
    register is read only when local custody leaves a path unmatched, and
    evidence that cannot be read or verified is an advisory, never a pass.

    A ``custody`` that pins a packet replaces the register: the packet is read
    again with the lane's whole current state, staged entries included, after
    every local reading, and decides even when local custody alone would pass.
    """
    git = _GitReads(worktree.path, run=run, environment=environment)
    scan = _untracked_scan(worktree, run=run, environment=environment)
    check = _CustodyCheck(worktree.path, git.object_format)
    held = {CUSTODY_BUILD_OUTPUT: list(scan.build_output), CUSTODY_EMPTY_SCAFFOLD: list(scan.empty)}
    master = (_TreeSource(CUSTODY_CURRENT_MASTER, git.tree(integration_head)),)
    unheld = (
        check.unheld(_at_risk_paths(worktree, run=run, environment=environment), master, held),
        check.unheld(scan.leaves, master, held),
    )
    diverging, residue, advisories, refusal = _custody_held(git, check, worktree, custody, unheld, held, integration_head)
    for advisory in advisories:
        _LOGGER.warning("lane worktree custody advisory for %s: %s", worktree.path, advisory)
    unlanded = _unlanded_commit_count(
        worktree,
        integration_head=integration_head,
        run=run,
        environment=environment,
    )
    summary = tuple((label, tuple(sorted(paths))) for label, paths in held.items() if paths)
    offending = tuple(sorted({*diverging, *residue}))
    if offending or refusal:
        return LaneWorktreeDisposability(
            disposable=False,
            offending_paths=offending,
            reason=_not_disposable_reason(diverging, residue, integration_head, advisories, refusal),
            unlanded_commits=unlanded,
            custody=summary,
            advisories=advisories,
        )
    return LaneWorktreeDisposability(
        disposable=True,
        offending_paths=(),
        reason=_disposable_reason(summary),
        unlanded_commits=unlanded,
        custody=summary,
        advisories=advisories,
    )


def remove_lane_worktree(
    worktree: LaneWorktree,
    *,
    run: GitRun = subprocess.run,
    environment: Mapping[str, str] | None = None,
    custody: RetirementCustodyContext | None = None,
) -> None:
    """Remove one proved-disposable lane worktree; preserve its branch forever.

    The force Git needs to clear declared build output and recoverable bytes is
    earned by the predicate above, evaluated here against the current files and
    freshly read custody, and is unreachable without it, so no caller chooses it.
    """
    _assert_worktree_shape(worktree)
    if not worktree.path.exists():
        return
    verdict = lane_worktree_disposability(worktree, run=run, environment=environment, custody=custody)
    if not verdict.disposable:
        raise LaneWorktreeNotDisposableError(worktree.path, verdict)
    _remove_exact_worktree(
        worktree,
        run=run,
        environment=environment,
        suppress=False,
        force=True,
    )


def sweep_orphaned_lane_worktrees(
    repo_root: Path,
    *,
    terminal_paths: Iterable[Path],
    run: GitRun = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> LaneWorktreeSweep:
    """Remove registered, terminal, provably disposable lane worktrees only.

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
    terminal = {path.resolve() for path in terminal_paths}
    registered = _registered_worktree_paths(
        repo_root.resolve(),
        run=run,
        environment=environment,
    )
    removed: list[Path] = []
    skipped: list[DirtyStaleWorktreeSkippedWarning] = []
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        _assert_under_root(candidate, root)
        resolved = candidate.resolve()
        if resolved not in registered:
            continue
        if resolved not in terminal:
            continue
        worktree = LaneWorktree(
            repo_root=repo_root.resolve(),
            root=root.resolve(),
            path=resolved,
            branch="",
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
    NUL-delimited, rename-free output names every touched path exactly.
    """
    result = _run_git(
        worktree.path,
        ("diff", "--name-only", "-z", "--no-renames", "HEAD"),
        run=run,
        environment=environment,
    )
    return tuple(sorted({path for path in result.stdout.split("\0") if path}))


@dataclass(frozen=True, slots=True)
class _UntrackedScan:
    """Untracked entries split into declared build output, empty roots and leaves."""

    build_output: tuple[str, ...]
    empty: tuple[str, ...]
    leaves: tuple[str, ...]


def _untracked_scan(
    worktree: LaneWorktree,
    *,
    run: GitRun,
    environment: Mapping[str, str] | None,
) -> _UntrackedScan:
    """Every untracked entry, judged by the declared allowlist rather than ignores.

    ``--exclude-standard`` is deliberately absent: honouring ``.gitignore`` here
    would equate "Git should not track this" with "safe to delete", and an
    ignore file routinely hides locally-generated config that is neither.
    ``--directory`` collapses a wholly-untracked tree to its root so a declared
    build cache costs one entry.  Any other collapsed root is expanded to its
    leaves by Git, which never descends a symlink: nested caches are recognised,
    a unique file is named exactly, and a root with no leaf holds no bytes.
    """
    build_output: list[str] = []
    empty: list[str] = []
    leaves: list[str] = []
    for entry in _untracked_entries(worktree, ("--directory",), run=run, environment=environment):
        if _is_ignorable_untracked(entry):
            build_output.append(entry)
        elif not entry.endswith("/"):
            leaves.append(entry)
        else:
            expanded = _untracked_entries(worktree, ("--", entry), run=run, environment=environment)
            if not expanded:
                empty.append(entry)
            for leaf in expanded:
                (build_output if _is_ignorable_untracked(leaf) else leaves).append(leaf)
    return _UntrackedScan(tuple(build_output), tuple(empty), tuple(leaves))


def _untracked_entries(
    worktree: LaneWorktree,
    arguments: tuple[str, ...],
    *,
    run: GitRun,
    environment: Mapping[str, str] | None,
) -> tuple[str, ...]:
    result = _run_git(
        worktree.path,
        ("--literal-pathspecs", "ls-files", "-z", "--others", *arguments),
        run=run,
        environment=environment,
    )
    return tuple(sorted({entry for entry in result.stdout.split("\0") if entry}))


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
    diverging: tuple[str, ...],
    residue: tuple[str, ...],
    integration_head: str,
    advisories: tuple[str, ...],
    refusal: str = "",
) -> str:
    """Name the paths that refused disposal; a generic refusal teaches nothing."""
    clauses = [f"pinned custody packet does not hold the lane: {refusal}"] if refusal else []
    if diverging:
        clauses.append(
            f"uncommitted tracked changes with no exact copy in {integration_head} or verified custody: {_render_paths(diverging)}",
        )
    if residue:
        clauses.append(
            f"untracked paths outside the declared build-output allowlist with no exact copy: {_render_paths(residue)}",
        )
    if advisories:
        clauses.append(f"custody advisories: {'; '.join(advisories)}")
    return "; ".join(clauses)


def _disposable_reason(summary: tuple[tuple[str, tuple[str, ...]], ...]) -> str:
    """Say which class each discarded path belonged to, under the same bound."""
    if not summary:
        return "no uncommitted or untracked path holds bytes"
    return "every uncommitted or untracked path is recoverable or reproducible: " + "; ".join(f"{label} {_render_paths(paths)}" for label, paths in summary)


def _render_paths(paths: tuple[str, ...]) -> str:
    """List the offending paths under a bound, keeping the elided count honest."""
    shown = ", ".join(paths[:_REPORTED_PATH_LIMIT])
    remainder = len(paths) - _REPORTED_PATH_LIMIT
    return shown if remainder <= 0 else f"{shown} (+{remainder} more)"


class _GitReads:
    """Read-only plumbing for one worktree under the caller's run and environment."""

    def __init__(self, cwd: Path, *, run: GitRun, environment: Mapping[str, str] | None) -> None:
        self._cwd = cwd
        self._run = run
        self._environment = environment
        self.object_format = self._text("rev-parse", "--show-object-format").strip()

    def tree(self, revision: str) -> dict[str, tuple[int, str]]:
        """Every entry of a commit's whole tree as ``path -> (mode, object id)``."""
        entries: dict[str, tuple[int, str]] = {}
        for record in self._text("ls-tree", "-r", "-z", "--full-tree", revision).split("\0"):
            if record:
                meta, _, path = record.partition("\t")
                mode, _, oid = meta.split(" ")
                entries[path] = (int(mode, 8), oid)
        return entries

    def reachable(self, commit: str, *heads: str) -> bool:
        """Whether an existing commit is an ancestor of any head, so Git retains it."""
        if self._optional("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}") is None:
            return False
        return any(self._optional("merge-base", "--is-ancestor", commit, head) is not None for head in heads)

    def tree_of(self, commit: str) -> str:
        return (self._optional("rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}") or "").strip()

    def common_dir(self) -> Path:
        return Path(self._text("rev-parse", "--path-format=absolute", "--git-common-dir").strip())

    def _text(self, *arguments: str) -> str:
        return _run_git(self._cwd, arguments, run=self._run, environment=self._environment).stdout

    def _optional(self, *arguments: str) -> str | None:
        """Stdout on exit 0, None on Git's answer "no" (exit 1), a fault otherwise."""
        result = self._run(
            ("git", "-C", str(self._cwd), *arguments),
            check=False,
            capture_output=True,
            text=True,
            env=_git_environment(self._environment),
        )
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 1:
            return None
        detail = (result.stderr or result.stdout).strip()
        raise LaneWorktreeError(f"git {' '.join(arguments)} failed: {detail}")


@dataclass(frozen=True, slots=True)
class _TreeSource:
    """A whole commit tree, so a path it does not list is recorded as absent."""

    label: str
    entries: Mapping[str, tuple[int, str]]

    def claims(self, path: str) -> bool:
        return path in self.entries

    def holds(self, path: str, local: LocalEntry) -> bool:
        if not local.readable:
            return False
        expected = self.entries.get(path)
        if expected is None:
            return local.mode is None
        return (local.mode, local.oid) == expected


@dataclass(frozen=True, slots=True)
class _FinalSource:
    """Explicit per-path final entries; a path it does not list says nothing."""

    label: str
    entries: Mapping[str, tuple[FinalEntry, ...]]

    def claims(self, path: str) -> bool:
        return path in self.entries

    def holds(self, path: str, local: LocalEntry) -> bool:
        return any(entry.holds(local) for entry in self.entries.get(path, ()))


_Source = _TreeSource | _FinalSource


class _CustodyCheck:
    """Match each path to the first source holding its exact entry, for one call."""

    def __init__(self, root: Path, object_format: str) -> None:
        self._root = root
        self._object_format = object_format
        self._entries: dict[str, LocalEntry] = {}

    def unheld(
        self,
        paths: Iterable[str],
        sources: Sequence[_Source],
        held: dict[str, list[str]],
    ) -> tuple[str, ...]:
        """File each held path under its source's label and return the others."""
        unheld: list[str] = []
        for path in paths:
            label = self._holder(path, sources)
            if label is None:
                unheld.append(path)
            else:
                held.setdefault(label, []).append(path)
        return tuple(unheld)

    def _holder(self, path: str, sources: Sequence[_Source]) -> str | None:
        local = self._entry(path, digest=any(source.claims(path) for source in sources))
        return next((source.label for source in sources if source.holds(path, local)), None)

    def _entry(self, path: str, *, digest: bool) -> LocalEntry:
        """Hash a path only when some source has an entry its bytes could match."""
        cached = self._entries.get(path)
        if cached is not None and (not digest or cached.oid is not None or cached.mode is None):
            return cached
        entry = read_entry(self._root, path, object_format=self._object_format, digest=digest)
        self._entries[path] = entry
        return entry


def _register_sources(
    git: _GitReads,
    claims: RegisterCustody,
    pending: set[str],
    integration_head: str,
) -> tuple[tuple[_Source, ...], tuple[str, ...]]:
    """Admit register claims as sources only where Git objects and packet bytes agree."""
    sources: list[_Source] = []
    refuted: list[str] = []
    for commit in claims.landing_commits:
        if git.reachable(commit, integration_head):
            sources.append(_TreeSource(CUSTODY_LANDED_HISTORY, git.tree(commit)))
        else:
            refuted.append(f"retire_custody_landing_unreachable: {commit} is not an ancestor of {integration_head}")
    if claims.candidates:
        common_dir = git.common_dir()
        for candidate in claims.candidates:
            sources.extend(_candidate_sources(git, candidate, common_dir, pending, integration_head))
    if claims.documents:
        sources.append(_FinalSource(CUSTODY_REGISTERED_DOCUMENT, claims.documents))
    return tuple(sources), tuple(refuted)


def _custody_held(
    git: _GitReads,
    check: _CustodyCheck,
    worktree: LaneWorktree,
    custody: RetirementCustodyContext | None,
    unheld: tuple[tuple[str, ...], tuple[str, ...]],
    held: dict[str, list[str]],
    integration_head: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str]:
    """The tracked and untracked paths still unheld after a retirement's own custody, its advisories and any packet refusal.

    A pinned packet replaces the register and always runs; otherwise the
    register is read only when local custody left a path unmatched.
    """
    diverging, residue = unheld
    if custody is None:
        return diverging, residue, (), ""
    if custody.packet is not None:
        refusal = _admit_packet(worktree, custody.packet, (*diverging, *residue), held)
        return (diverging, residue, (), refusal) if refusal else ((), (), (), "")
    if not (diverging or residue):
        return diverging, residue, (), ""
    claims = custody.read(worktree.repo_root)
    sources, refuted = _register_sources(git, claims, {*diverging, *residue}, integration_head)
    return check.unheld(diverging, sources, held), check.unheld(residue, sources, held), (*claims.advisories, *refuted), ""


def _admit_packet(worktree: LaneWorktree, packet: PacketCustody, pending: tuple[str, ...], held: dict[str, list[str]]) -> str:
    """Hold every pending path by a pinned packet that holds the whole lane, or say why not.

    Pass condition: the packet's verifier finds it intact, bound to this lane,
    at the expected fingerprint, and a complete current inventory equal to it.
    Anything else keeps every path, whatever local custody would allow.
    """
    if packet.lane.resolve() != worktree.path.resolve():
        return f"custody_packet_target_mismatch: packet {packet.packet_id} binds {packet.lane}, not {worktree.path}"
    verdict = packet.verify()
    if not verdict.holds:
        return f"{verdict.code}: packet {packet.packet_id}: {verdict.detail}"
    held.setdefault(CUSTODY_VERIFIED_PACKET, []).extend(pending)
    return ""


def _candidate_sources(
    git: _GitReads,
    candidate: CandidateCustody,
    common_dir: Path,
    pending: set[str],
    integration_head: str,
) -> list[_Source]:
    """A candidate's retained commit tree, plus each packet entry still on disk.

    Candidate custody proves the bytes are recoverable, not that they landed.
    """
    sources: list[_Source] = []
    if candidate.commit_sha and git.tree_of(candidate.commit_sha) == candidate.tree_sha and git.reachable(candidate.commit_sha, integration_head, "HEAD"):
        sources.append(_TreeSource(CUSTODY_FROZEN_CANDIDATE, git.tree(candidate.commit_sha)))
    packet = candidate_packet(common_dir, candidate)
    verified = {path: (entry,) for path, entry in candidate.entries.items() if path in pending and _packet_holds(packet, path, entry, git.object_format)}
    if verified:
        sources.append(_FinalSource(CUSTODY_FROZEN_CANDIDATE, verified))
    return sources


def _packet_holds(packet: Path, path: str, entry: FinalEntry, object_format: str) -> bool:
    """A present final entry counts only while its packet file holds those exact bytes."""
    if entry.mode is None:
        return True
    return entry.holds(read_entry(packet, path, object_format=object_format, digest=True))


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
    return {key: value for key, value in source.items() if not key.startswith(_GIT_ENV_PREFIX) or key in _GIT_ENV_EXCEPTIONS}


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
        _assert_shared_venv_link_owned(worktree)
        _run_git(
            worktree.repo_root,
            ("worktree", "remove", *flags, str(worktree.path)),
            run=run,
            environment=environment,
        )
        # Git owns the decision to remove a registered worktree.  In
        # particular, a lock refusal must leave the provisioned interpreter
        # usable; only unlink the owned convenience link after Git accepted
        # the teardown.
        _remove_shared_venv_link(worktree)
    except LaneWorktreeError:
        if not suppress:
            raise


def _remove_shared_venv_link(worktree: LaneWorktree) -> None:
    """Discard only the provisioning-owned venv link before clean teardown."""
    _assert_shared_venv_link_owned(worktree)
    link = worktree.path / ".venv"
    if link.exists() or link.is_symlink():
        link.unlink()


def _assert_shared_venv_link_owned(worktree: LaneWorktree) -> None:
    """Refuse a foreign `.venv` before Git receives a destructive request."""
    link = worktree.path / ".venv"
    if not link.exists() and not link.is_symlink():
        return
    shared_venv = (worktree.repo_root / ".venv").resolve()
    if not link.is_symlink() or link.resolve() != shared_venv:
        raise LaneWorktreeError(f"refusing to remove non-provisioned venv entry: {link}")


def _registered_worktree_paths(
    repo_root: Path,
    *,
    run: GitRun,
    environment: Mapping[str, str] | None,
) -> set[Path]:
    result = _run_git(
        repo_root,
        ("worktree", "list", "--porcelain"),
        run=run,
        environment=environment,
    )
    return {Path(line.removeprefix("worktree ")).resolve() for line in result.stdout.splitlines() if line.startswith("worktree ")}
