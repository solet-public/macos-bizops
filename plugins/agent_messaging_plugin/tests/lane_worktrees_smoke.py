"""Behavioral proof for per-lane worktree isolation and exact teardown."""

from __future__ import annotations

import errno
import hashlib
import importlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import agent_messaging_plugin.lane_worktrees as lane_worktrees  # noqa: E402
from agent_messaging_plugin.lane_worktrees import (  # noqa: E402
    DirtyStaleWorktreeSkippedWarning,
    LaneWorktree,
    LaneWorktreeError,
    lane_worktree_disposability,
    lane_worktree_for,
    provision_lane_worktree,
    remove_lane_worktree,
    sweep_orphaned_lane_worktrees,
    worktree_pythonpath,
)
from agent_messaging_plugin.register_unit_client import RegisteredRevision, UnitAssignment  # noqa: E402

_CUSTODY_ENGINE = "agent_messaging_plugin.lane_worktree_preservation"


def _environment(root: Path) -> dict[str, str]:
    """A Git environment that cannot redirect the fixture into a real checkout."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")} | {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _run_git(root: Path, *arguments: str, environment: dict[str, str]) -> str:
    root = root.resolve()
    assert root.is_relative_to(root.parent), root
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


def _fixture(root: Path, environment: dict[str, str]) -> Path:
    repo = root / "fixture-repo"
    repo.mkdir(parents=True)
    _run_git(repo, "init", "--initial-branch", "master", environment=environment)
    _run_git(repo, "config", "user.email", "fixture@example.invalid", environment=environment)
    _run_git(repo, "config", "user.name", "fixture", environment=environment)
    (repo / ".venv").mkdir()
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt", environment=environment)
    _run_git(repo, "commit", "-m", "fixture base", environment=environment)
    return repo


def _assert_shared_dirt_isolation(root: Path, environment: dict[str, str]) -> int:
    repo = _fixture(root, environment)
    lane = lane_worktree_for(
        repo,
        role_name="lane-isolation",
        agent_instance_id="agi-isolation",
    )
    provision_lane_worktree(lane, environment=environment)
    (repo / "shared-only.txt").write_text("shared dirt\n", encoding="utf-8")
    assert not (lane.path / "shared-only.txt").exists()
    (lane.path / "lane-only.txt").write_text("lane bytes\n", encoding="utf-8")
    _run_git(lane.path, "add", "lane-only.txt", environment=environment)
    _run_git(lane.path, "commit", "-m", "lane commit", environment=environment)
    committed = _run_git(lane.path, "show", "--format=", "--name-only", "HEAD", environment=environment)
    assert committed.splitlines() == ["lane-only.txt"], committed
    assert (lane.path / ".venv").is_symlink()
    assert worktree_pythonpath(lane.path, "inherited") == os.pathsep.join(
        (str(lane.path.resolve()), "inherited"),
    )
    remove_lane_worktree(lane, environment=environment)
    assert not lane.path.exists()
    _run_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{lane.branch}", environment=environment)
    return 6


def _assert_red_pre_fix_shape(root: Path, environment: dict[str, str]) -> int:
    repo = _fixture(root, environment)
    lane = lane_worktree_for(repo, role_name="lane-red", agent_instance_id="agi-red")
    provision_lane_worktree(lane, environment=environment)
    (repo / "shared-only.txt").write_text("shared dirt\n", encoding="utf-8")
    failed = False
    try:
        assert (lane.path / "shared-only.txt").exists(), "red: shared dirt leaked into lane tree"
    except AssertionError:
        failed = True
    assert failed, "the red proof must fail when it expects shared dirt in the worktree"
    remove_lane_worktree(lane, environment=environment)
    return 2


def _assert_master_base_with_feature_checked_out(
    root: Path,
    environment: dict[str, str],
) -> int:
    repo = _fixture(root, environment)
    master_head = _run_git(repo, "rev-parse", "master", environment=environment).strip()
    _run_git(repo, "checkout", "-b", "fixture-feature", environment=environment)
    (repo / "feature-only.txt").write_text("feature branch only\n", encoding="utf-8")
    _run_git(repo, "add", "feature-only.txt", environment=environment)
    _run_git(repo, "commit", "-m", "fixture feature", environment=environment)

    lane = lane_worktree_for(
        repo,
        role_name="lane-master-base",
        agent_instance_id="agi-master-base",
    )
    provision_lane_worktree(lane, environment=environment)
    lane_head = _run_git(lane.path, "rev-parse", "HEAD", environment=environment).strip()
    assert lane_head == master_head, "lane must start at master while shared HEAD is fixture-feature"
    assert not (lane.path / "feature-only.txt").exists(), "feature-only commit must not enter lane"
    assert (lane.path / ".venv").is_symlink(), "master-based lane keeps shared interpreter link"
    return 3


def _assert_missing_master_uses_head_with_warning(
    root: Path,
    environment: dict[str, str],
) -> int:
    repo = _fixture(root, environment)
    _run_git(repo, "branch", "-m", "fixture-main", environment=environment)
    expected_head = _run_git(repo, "rev-parse", "HEAD", environment=environment).strip()
    lane = lane_worktree_for(
        repo,
        role_name="lane-missing-master",
        agent_instance_id="agi-missing-master",
    )

    class WarningCapture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.WARNING)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    logger = logging.getLogger(lane_worktrees.__name__)
    handler = WarningCapture()
    logger.addHandler(handler)
    try:
        provision_lane_worktree(lane, environment=environment)
    finally:
        logger.removeHandler(handler)

    lane_head = _run_git(lane.path, "rev-parse", "HEAD", environment=environment).strip()
    assert lane_head == expected_head, "missing master must provision from HEAD"
    warning_messages = [record.getMessage() for record in handler.records if record.levelno == logging.WARNING]
    warning = "\n".join(warning_messages)
    assert warning_messages and "master" in warning and "HEAD" in warning and str(repo.resolve()) in warning, f"missing master fallback must be visible: {warning}"
    return 2


def _assert_sweep_is_exact(root: Path, environment: dict[str, str]) -> int:
    repo = _fixture(root, environment)
    live = lane_worktree_for(repo, role_name="lane-live", agent_instance_id="agi-live")
    orphan = lane_worktree_for(repo, role_name="lane-orphan", agent_instance_id="agi-orphan")
    provision_lane_worktree(live, environment=environment)
    provision_lane_worktree(orphan, environment=environment)
    sweep = sweep_orphaned_lane_worktrees(
        repo,
        terminal_paths=(orphan.path,),
        environment=environment,
    )
    assert sweep.removed == (orphan.path.resolve(),), sweep
    assert sweep.skipped == (), sweep
    assert live.path.exists()
    _run_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{orphan.branch}", environment=environment)
    remove_lane_worktree(live, environment=environment)
    return 5


def _assert_lock_refusal_preserves_shared_venv(root: Path, environment: dict[str, str]) -> int:
    """A lock refusal must retain the provisioned interpreter link.

    Red mutation: move ``_remove_shared_venv_link`` back before Git's removal
    request. Git then refuses the locked worktree while its `.venv` is already
    missing, breaking a still-live lane without removing it.
    """
    repo = _fixture(root, environment)
    lane = lane_worktree_for(repo, role_name="lane-locked", agent_instance_id="agi-locked")
    provision_lane_worktree(lane, environment=environment)
    link = lane.path / ".venv"
    _run_git(repo, "worktree", "lock", str(lane.path), environment=environment)
    refused = False
    try:
        remove_lane_worktree(lane, environment=environment)
    except LaneWorktreeError:
        refused = True
    assert refused, "a locked worktree removal must be refused"
    assert link.is_symlink(), "a lock refusal must preserve the provisioned .venv link"
    _run_git(repo, "worktree", "unlock", str(lane.path), environment=environment)
    remove_lane_worktree(lane, environment=environment)
    assert not lane.path.exists(), "an unlocked disposable lane still removes normally"
    return 3


def _assert_foreign_venv_refuses_before_git_removal(
    root: Path,
    environment: dict[str, str],
) -> int:
    """A foreign `.venv` must refuse before the earned force reaches Git."""
    repo = _fixture(root, environment)
    lane = lane_worktree_for(repo, role_name="lane-foreign-venv", agent_instance_id="agi-foreign")
    provision_lane_worktree(lane, environment=environment)
    link = lane.path / ".venv"
    link.unlink()
    link.mkdir()
    refused = False
    try:
        remove_lane_worktree(lane, environment=environment)
    except LaneWorktreeError:
        refused = True
    assert refused, "a non-provisioned .venv must refuse removal"
    assert lane.path.exists() and link.is_dir(), "foreign .venv survives the refusal"
    link.rmdir()
    link.symlink_to(repo / ".venv", target_is_directory=True)
    remove_lane_worktree(lane, environment=environment)
    return 2


def _advance_master(repo: Path, name: str, body: str, environment: dict[str, str]) -> None:
    """Land one commit on the integration head after a lane is already checked out."""
    (repo / name).write_text(body, encoding="utf-8")
    _run_git(repo, "add", name, environment=environment)
    _run_git(repo, "commit", "-m", f"master advances {name}", environment=environment)


def _lane_at_base(
    root: Path,
    environment: dict[str, str],
    name: str,
) -> tuple[Path, LaneWorktree]:
    """One fixture repo plus one provisioned lane worktree still at its own base."""
    repo = _fixture(root, environment)
    lane = lane_worktree_for(repo, role_name=name, agent_instance_id=f"agi-{name}")
    provision_lane_worktree(lane, environment=environment)
    return repo, lane


def _assert_sweep_tolerates_a_stuck_orphan(root: Path, environment: dict[str, str]) -> int:
    """D1: opportunistic tidying must never fail the operation it is tidying for.

    Red mutation: restore ``suppress=False`` removal inside the sweep loop, so the
    stuck orphan aborts the sweep and the following provision never happens.
    """
    repo, stuck = _lane_at_base(root, environment, "lane-stuck")
    clean = lane_worktree_for(repo, role_name="lane-clean", agent_instance_id="agi-clean")
    provision_lane_worktree(clean, environment=environment)
    (stuck.path / "tracked.txt").write_text("divergent lane bytes\n", encoding="utf-8")

    sweep = sweep_orphaned_lane_worktrees(
        repo,
        terminal_paths=(stuck.path, clean.path),
        environment=environment,
    )

    assert sweep.removed == (clean.path.resolve(),), sweep
    assert len(sweep.skipped) == 1, sweep
    warning = sweep.skipped[0]
    assert isinstance(warning, DirtyStaleWorktreeSkippedWarning), warning
    assert warning.code == "dirty_stale_worktree_skipped", warning
    assert warning.path == stuck.path.resolve(), warning
    assert "tracked.txt" in warning.git_diagnostic, warning
    assert stuck.path.exists(), "a skipped orphan must survive the sweep intact"

    fresh = lane_worktree_for(repo, role_name="lane-fresh", agent_instance_id="agi-fresh")
    provision_lane_worktree(fresh, environment=environment)
    assert fresh.path.is_dir(), "a stuck orphan must not block a later provision"
    return 6


def _assert_disposability_predicate(root: Path, environment: dict[str, str]) -> int:
    """D2: decide disposability in Git's own diff semantics, and name refusals.

    Red mutations: compare content without mode and kind and (iv), (v), (vi) and
    the untracked type and mode controls pass while destroying real state;
    unscope it from the at-risk set to the whole tree and (vii) flips to
    not-disposable, so the sweep silently stops removing anything; collapse
    untracked roots again and the nested cache refuses; declare all of
    ``profile`` disposable and its unique file is lost; drop the untracked
    exact-entry comparison and a file already on master refuses.
    """
    cases = (
        ("matching", _case_modified_but_matching, True, ()),
        ("diverging", _case_modified_and_diverging, False, ("tracked.txt",)),
        ("caches", _case_untracked_caches_only, True, ()),
        ("mode", _case_mode_only_change, False, ("tracked.txt",)),
        ("symlink", _case_retargeted_symlink, False, ("link",)),
        ("deleted", _case_tracked_deletion, False, ("tracked.txt",)),
        ("deletion-landed", _case_deletion_already_landed, True, ()),
        ("stale-base", _case_stale_base_unrelated_movement, True, ()),
        ("nested-cache", _case_nested_cache_in_new_package, True, ()),
        ("nested-unique", _case_unique_leaf_beside_nested_cache, False, ("newpkg/notes.txt",)),
        ("profile-unique", _case_nonempty_profile_state, False, ("profile/data/state.db",)),
        ("untracked-master", _case_untracked_equal_to_master, True, ()),
        ("untracked-bytes", _case_untracked_differs_from_master, False, ("landed.py",)),
        ("untracked-mode", _case_untracked_mode_differs_from_master, False, ("landed.py",)),
        ("untracked-type", _case_untracked_link_where_master_has_file, False, ("landed.py",)),
        ("filter-raw", _case_raw_bytes_differ_behind_text_filter, False, ("tracked.txt",)),
        ("filter-exact", _case_raw_bytes_equal_behind_text_filter, True, ()),
    )
    for name, build, expected, offending in cases:
        repo, lane = _lane_at_base(root / name, environment, f"lane-{name}")
        build(repo, lane, environment)
        verdict = lane_worktree_disposability(lane, environment=environment)
        assert verdict.disposable is expected, (name, verdict)
        assert verdict.offending_paths == offending, (name, verdict)
        for path in offending:
            assert path in verdict.reason, (name, verdict.reason)
    return len(cases)


def _case_modified_but_matching(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """(i) The lane's uncommitted bytes are exactly what already landed."""
    _advance_master(repo, "tracked.txt", "landed\n", environment)
    (lane.path / "tracked.txt").write_text("landed\n", encoding="utf-8")


def _case_modified_and_diverging(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """(ii) Uncommitted bytes that exist nowhere else must refuse, by name."""
    (lane.path / "tracked.txt").write_text("only here\n", encoding="utf-8")


def _case_untracked_caches_only(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """(iii) Declared build output is residue, not state."""
    for directory, filename in (
        (".ruff_cache", "cache.bin"),
        ("__pycache__", "mod.pyc"),
        ("solet.egg-info", "PKG-INFO"),
    ):
        (lane.path / directory).mkdir()
        (lane.path / directory / filename).write_text("residue\n", encoding="utf-8")


def _assert_build_output_allowlist(root: Path, environment: dict[str, str]) -> int:
    """`build/` is reproducible setuptools output, not a `.gitignore` inference.

    Red mutation: remove ``build`` from the declared allowlist.  The fixture then
    becomes non-disposable and names ``build/`` as its refused residue.
    """
    repo, lane = _lane_at_base(root, environment, "lane-build-output")
    for directory, filename in (
        ("build/bdist.macosx-26.0-arm64", "placeholder"),
        ("build/lib", "package.py"),
    ):
        destination = lane.path / directory
        destination.mkdir(parents=True)
        (destination / filename).write_text("setuptools output\n", encoding="utf-8")

    accepted = lane_worktree_disposability(lane, environment=environment)
    assert accepted.disposable, accepted

    declared = lane_worktrees._DISPOSABLE_UNTRACKED_DIRECTORIES
    try:
        lane_worktrees._DISPOSABLE_UNTRACKED_DIRECTORIES = declared - {"build"}
        refused = lane_worktree_disposability(lane, environment=environment)
    finally:
        lane_worktrees._DISPOSABLE_UNTRACKED_DIRECTORIES = declared
    assert not refused.disposable, refused
    leaves = ("build/bdist.macosx-26.0-arm64/placeholder", "build/lib/package.py")
    assert refused.offending_paths == leaves, refused
    assert all(leaf in refused.reason for leaf in leaves), refused.reason
    return 4


def test_empty_profile_scaffold_is_disposable(root: Path, environment: dict[str, str]) -> int:
    """Gate runs leave an empty untracked profile/data/logs tree; it holds no bytes.

    Red on the reviewed base: ``--directory`` collapses the tree to ``profile/``,
    which no allowlist entry names, so an empty scaffold refused retirement.
    """
    _, lane = _lane_at_base(root, environment, "lane-empty-profile")
    (lane.path / "profile" / "data" / "logs").mkdir(parents=True)
    verdict = lane_worktree_disposability(lane, environment=environment)
    assert verdict.disposable, verdict
    assert verdict.offending_paths == (), verdict
    remove_lane_worktree(lane, environment=environment)
    assert not lane.path.exists(), "an empty scaffold must not block exact removal"
    return 3


def test_ahead_branch_survives_removal(root: Path, environment: dict[str, str]) -> int:
    """D1: removal never deletes or moves the lane branch; the ahead count is advisory."""
    repo, lane = _lane_at_base(root, environment, "lane-ahead")
    (lane.path / "lane-only.txt").write_text("committed lane bytes\n", encoding="utf-8")
    _run_git(lane.path, "add", "lane-only.txt", environment=environment)
    _run_git(lane.path, "commit", "-m", "lane commit ahead of master", environment=environment)
    ref = f"refs/heads/{lane.branch}"
    before = _run_git(repo, "rev-parse", ref, environment=environment).strip()
    verdict = lane_worktree_disposability(lane, environment=environment)
    assert verdict.disposable and verdict.unlanded_commits == 1, verdict
    remove_lane_worktree(lane, environment=environment)
    assert not lane.path.exists(), "a disposable ahead lane is removed"
    after = _run_git(repo, "rev-parse", ref, environment=environment).strip()
    assert after == before, (before, after)
    return 3


def _assert_custody_classes_are_named(root: Path, environment: dict[str, str]) -> int:
    """Every discarded path is reported under the class that made it safe."""
    repo, lane = _lane_at_base(root, environment, "lane-classes")
    _advance_master(repo, "landed.py", "print('landed')\n", environment)
    (lane.path / "landed.py").write_text("print('landed')\n", encoding="utf-8")
    (lane.path / "profile" / "data" / "logs").mkdir(parents=True)
    (lane.path / "__pycache__").mkdir()
    (lane.path / "__pycache__" / "landed.pyc").write_bytes(b"\x00cache")
    verdict = lane_worktree_disposability(lane, environment=environment)
    assert verdict.disposable, verdict
    assert dict(verdict.custody) == {
        "build_output": (".venv", "__pycache__/"),
        "empty_scaffold": ("profile/",),
        "current_master": ("landed.py",),
    }, verdict.custody
    assert all(label in verdict.reason for label in ("build_output", "empty_scaffold", "current_master")), verdict.reason
    return 3


def _case_deletion_already_landed(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """A tracked deletion the integration head already made loses nothing."""
    _run_git(repo, "rm", "--quiet", "tracked.txt", environment=environment)
    _run_git(repo, "commit", "-m", "master deletes tracked.txt", environment=environment)
    (lane.path / "tracked.txt").unlink()


def _case_nested_cache_in_new_package(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """A collapsed untracked root holding only a nested cache holds no bytes."""
    (lane.path / "newpkg" / "__pycache__").mkdir(parents=True)
    (lane.path / "newpkg" / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"\x00cache")


def _case_unique_leaf_beside_nested_cache(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """The same root with one unique leaf refuses by that leaf alone."""
    _case_nested_cache_in_new_package(repo, lane, environment)
    (lane.path / "newpkg" / "notes.txt").write_text("only here\n", encoding="utf-8")


def _case_nonempty_profile_state(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """Runtime state is protected by its bytes, never declared harmless by location."""
    (lane.path / "profile" / "data" / "logs").mkdir(parents=True)
    (lane.path / "profile" / "data" / "state.db").write_bytes(b"unique runtime state")


def _case_untracked_equal_to_master(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """An untracked file byte-identical to the integration head's entry lost nothing."""
    _advance_master(repo, "landed.py", "print('landed')\n", environment)
    (lane.path / "landed.py").write_text("print('landed')\n", encoding="utf-8")


def _case_untracked_differs_from_master(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    _advance_master(repo, "landed.py", "print('landed')\n", environment)
    (lane.path / "landed.py").write_text("print('edited after landing')\n", encoding="utf-8")


def _case_untracked_mode_differs_from_master(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """Identical bytes with an exec bit master lacks are not that entry."""
    _case_untracked_equal_to_master(repo, lane, environment)
    (lane.path / "landed.py").chmod(0o755)


def _case_untracked_link_where_master_has_file(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """A symlink whose target text equals master's file bytes shares its blob id only."""
    _advance_master(repo, "landed.py", "target.txt", environment)
    (lane.path / "landed.py").symlink_to("target.txt")


def _commit_text_filter(repo: Path, lane: LaneWorktree, environment: dict[str, str]) -> None:
    """Give the lane an end-of-line filter, then land LF bytes on master."""
    _advance_master(repo, ".gitattributes", "*.txt text\n", environment)
    _run_git(lane.path, "merge", "--ff-only", "master", environment=environment)
    _advance_master(repo, "tracked.txt", "landed\n", environment)


def _case_raw_bytes_differ_behind_text_filter(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """CRLF bytes ``git diff`` normalises to master's LF blob still exist nowhere else."""
    _commit_text_filter(repo, lane, environment)
    (lane.path / "tracked.txt").write_bytes(b"landed\r\n")


def _case_raw_bytes_equal_behind_text_filter(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    _commit_text_filter(repo, lane, environment)
    (lane.path / "tracked.txt").write_bytes(b"landed\n")


def _case_mode_only_change(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """(iv) Identical bytes, changed exec bit; a content check would miss it."""
    (lane.path / "tracked.txt").chmod(0o755)


def _case_retargeted_symlink(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """(v) Link values differ while both targets' contents are identical."""
    for name in ("alpha.txt", "beta.txt"):
        (repo / name).write_text("identical\n", encoding="utf-8")
    (repo / "link").symlink_to("alpha.txt")
    _run_git(repo, "add", "alpha.txt", "beta.txt", "link", environment=environment)
    _run_git(repo, "commit", "-m", "master tracks a symlink", environment=environment)
    _run_git(lane.path, "merge", "--ff-only", "master", environment=environment)
    (lane.path / "link").unlink()
    (lane.path / "link").symlink_to("beta.txt")


def _case_tracked_deletion(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """(vi) A tracked file deleted in the worktree is real unlanded state."""
    (lane.path / "tracked.txt").unlink()


def _case_stale_base_unrelated_movement(
    repo: Path,
    lane: LaneWorktree,
    environment: dict[str, str],
) -> None:
    """(vii) Master moved in other lanes' paths; that is the normal case."""
    _advance_master(repo, "unrelated.txt", "another lane landed this\n", environment)
    _advance_master(repo, "tracked.txt", "landed\n", environment)
    (lane.path / "tracked.txt").write_text("landed\n", encoding="utf-8")


def _engine() -> ModuleType | None:
    """The custody engine, or None while it does not exist, so a red run is an assertion.

    Only the engine module's own absence counts; a missing dependency of the
    engine still raises, because that is a defect, not a missing capability.
    """
    try:
        return importlib.import_module(_CUSTODY_ENGINE)
    except ModuleNotFoundError as exc:
        if exc.name != _CUSTODY_ENGINE:
            raise
        return None


def _assignment(unit_id: str = "unt_fixture", repository_id: str = "rep_fixture") -> UnitAssignment:
    """A registered assignment whose revision bytes verify, as a session row stores it."""
    text = f"fixture brief for {unit_id}"
    raw = text.encode("utf-8")
    revision = RegisteredRevision("drv_fixture", hashlib.sha256(raw).hexdigest(), len(raw), text)
    return UnitAssignment(unit_id, f"{unit_id}-key", repository_id, revision)


def _custody_context(engine: ModuleType, repo: Path, name: str, **overrides: Any) -> Any:
    """The trusted identity of the lane ``_lane_at_base`` provisioned under ``name``.

    ``overrides`` builds a context that disagrees with a packet's binding, as a
    foreign repository or session would.
    """
    fields: dict[str, Any] = {
        "repo_root": repo,
        "lane_name": name,
        "agent_instance_id": f"agi-{name}",
        "assignment": _assignment(),
        "actor": "fixture-actor",
    }
    return engine.LaneCustodyContext(**(fields | overrides))


def _capture_response(repo: Path, name: str, environment: dict[str, str], **request: Any) -> dict[str, Any]:
    """Snapshot then capture through the engine, answering even when it is missing.

    The answer is a plain mapping so the red run on a base without the engine
    fails the capture assertion rather than an import.  ``request`` overrides
    the consented fingerprint, disposition, rationale, predecessor or writer.
    """
    engine = _engine()
    if engine is None:
        return {"success": False, "code": "custody_engine_missing", "result": None}
    context = _custody_context(engine, repo, name)
    snapshot = engine.snapshot_lane_custody(context, environment=environment)
    if not snapshot.success:
        return {"success": False, "code": snapshot.code, "detail": snapshot.detail, "result": snapshot}
    fields: dict[str, Any] = {
        "expected_fingerprint": snapshot.snapshot.fingerprint,
        "disposition": engine.DISPOSITION_PRESERVED,
        "rationale": "fixture evidence",
    }
    result = engine.capture_lane_custody(context, environment=environment, **(fields | request))
    return {"success": result.success, "code": result.code, "detail": result.detail, "engine": engine, "context": context, "snapshot": snapshot.snapshot, "result": result}


_LANE_BASE = ((".gitignore", b"*.log\n"), ("staged-only.txt", b"head\n"), ("gone.txt", b"deleted in the worktree\n"), ("run.sh", b"#!/bin/sh\n"))
_LANE_STAGED = (("tracked.txt", b"staged\n"), ("staged-only.txt", b"staged only\n"), ("added.txt", b"added\n"))
_LANE_UNSTAGED = (
    ("tracked.txt", b"unstaged\n"),
    ("staged-only.txt", b"head\n"),
    ("notes/deep/data.bin", b"\x00\xff\x10binary\x00"),
    ("notes/crlf.txt", b"line\r\nend\r\n"),
    ("notes/tail.txt", b"no final newline"),
    ("notes/debug.log", b"ignored but unique\n"),
    ("notes/tool", b"#!/bin/sh\nexit 0\n"),
    ("__pycache__/cache.pyc", b"\x00reproducible"),
)


def _write_files(root: Path, files: tuple[tuple[str, bytes], ...]) -> None:
    for relative, body in files:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(body)


def _custody_lane(root: Path, environment: dict[str, str], name: str) -> tuple[Path, LaneWorktree]:
    """A lane holding every kind of state custody must keep exactly.

    One file has separate staged and unstaged versions, one a staged change
    whose worktree equals HEAD, one is new and staged; a tracked file is
    deleted, another gains its exec bit and a tracked link is retargeted.
    Untracked and ignored binary, CRLF and unterminated bytes, an executable,
    empty directories and declared build output sit beside them.
    """
    repo, lane = _lane_at_base(root, environment, name)
    work = lane.path
    _write_files(work, _LANE_BASE)
    (work / "link").symlink_to("tracked.txt")
    _run_git(work, "add", *(path for path, _ in _LANE_BASE), "link", environment=environment)
    _run_git(work, "commit", "-m", "lane base for custody", environment=environment)
    _write_files(work, _LANE_STAGED)
    _run_git(work, "add", *(path for path, _ in _LANE_STAGED), environment=environment)
    _write_files(work, _LANE_UNSTAGED)
    (work / "gone.txt").unlink()
    (work / "run.sh").chmod(0o755)
    (work / "notes" / "tool").chmod(0o755)
    (work / "link").unlink()
    (work / "link").symlink_to("../outside-target")
    (work / "notes" / "empty-inner").mkdir()
    (work / "empty" / "nested").mkdir(parents=True)
    return repo, lane


def _tree_state(root: Path, relative: str = "") -> dict[str, tuple[str, bool, bytes]]:
    """Every leaf's kind, exec bit and raw bytes or link text, and every empty directory.

    Git administration, the provisioned interpreter link and declared build
    output are left out, as custody leaves them out.
    """
    state: dict[str, tuple[str, bool, bytes]] = {}
    for entry in sorted(os.scandir(root / relative), key=lambda item: item.name):
        path = f"{relative}{entry.name}"
        if path in {".git", ".venv", "__pycache__"}:
            continue
        if entry.is_symlink():
            state[path] = ("link", False, os.fsencode(os.readlink(entry.path)))
        elif entry.is_dir(follow_symlinks=False):
            state.update(_tree_state(root, f"{path}/") or {path: ("dir", False, b"")})
        else:
            executable = bool(entry.stat(follow_symlinks=False).st_mode & 0o100)
            state[path] = ("file", executable, Path(entry.path).read_bytes())
    return state


def _index_state(lane: LaneWorktree, environment: dict[str, str]) -> tuple[str, str]:
    """The index's exact entries with stages, and its per-entry flag tags.

    ``-s`` alone would hide assume-unchanged and skip-worktree, so a rebuild
    that dropped a flag must also differ here.
    """
    return (
        _run_git(lane.path, "ls-files", "-s", "-z", environment=environment),
        _run_git(lane.path, "ls-files", "-v", "-z", environment=environment),
    )


def _index_file(lane: LaneWorktree, environment: dict[str, str]) -> bytes:
    """The lane's raw index file, which capture must never rewrite, not even its stat data."""
    return Path(_run_git(lane.path, "rev-parse", "--path-format=absolute", "--git-path", "index", environment=environment).strip()).read_bytes()


def _payload(engine: ModuleType, context: Any, packet_id: str, path: str, layer: str, environment: dict[str, str]) -> bytes:
    """One selected entry's bytes, the only way custody hands bytes back.

    Each read reverifies the whole packet before it answers.
    """
    result = engine.read_custody_payload(context, packet_id, path, layer=layer, environment=environment)
    assert result.success and isinstance(result.payload, bytes), result
    return result.payload


def _write_leaf(target: Path, mode: int, data: bytes) -> None:
    """Restore one leaf as its Git mode says: a link of that text, or a file with or without exec."""
    if target.is_symlink() or target.exists():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == 0o120000:
        target.symlink_to(os.fsdecode(data))
        return
    target.write_bytes(data)
    target.chmod(0o755 if mode == 0o100755 else 0o644)


def _rebuild_index(engine: ModuleType, context: Any, lane: LaneWorktree, packet: Any, environment: dict[str, str]) -> None:
    """Write each staged blob back from packet bytes, then apply the recorded index delta."""
    manifest = packet.manifest
    lines: list[str] = []
    for record in manifest.index:
        if record.mode is None:
            lines.append(f"0 {'0' * len(manifest.head_tree)}\t{record.path}")
            continue
        data = _payload(engine, context, packet.packet_id, record.path, "index", environment)
        written = subprocess.run(("git", "-C", str(lane.path), "hash-object", "-w", "--stdin"), input=data, capture_output=True, env=environment, check=True)
        assert written.stdout.decode().strip() == record.oid, record
        lines.append(f"{record.mode:o} {record.oid}\t{record.path}")
    subprocess.run(("git", "-C", str(lane.path), "update-index", "-z", "--index-info"), input="".join(f"{line}\0" for line in lines).encode(), env=environment, check=True)


def _rebuild_worktree(engine: ModuleType, context: Any, lane: LaneWorktree, packet: Any, environment: dict[str, str]) -> None:
    """Paths clean against the index take its blob; every recorded worktree entry is exact."""
    manifest = packet.manifest
    changed = {record.path for record in manifest.worktree}
    for record in manifest.index:
        if record.mode is not None and record.path not in changed:
            _write_leaf(lane.path / record.path, record.mode, _payload(engine, context, packet.packet_id, record.path, "index", environment))
    for record in manifest.worktree:
        target = lane.path / record.path
        if record.kind == "absent":
            target.unlink(missing_ok=True)
        elif record.kind == "directory":
            target.mkdir(parents=True, exist_ok=True)
        else:
            _write_leaf(target, record.mode, _payload(engine, context, packet.packet_id, record.path, "worktree", environment))


def _assert_manifest_shape(manifest: Any) -> None:
    """Index and worktree state stay apart, every kind is exact, build output is only named."""
    kinds = {record.path: (record.kind, record.mode) for record in manifest.worktree}
    expected = {
        "gone.txt": ("absent", None),
        "run.sh": ("file", 0o100755),
        "link": ("symlink", 0o120000),
        "empty/nested": ("directory", None),
        "notes/empty-inner": ("directory", None),
        "added.txt": None,
    }
    shape = ({record.path for record in manifest.index}, {path: kinds.get(path) for path in expected}, {".venv", "__pycache__/"} <= set(manifest.build_output))
    assert shape == ({"added.txt", "staged-only.txt", "tracked.txt"}, expected, True), shape


def _custody_store(repo: Path, agent_instance_id: str, environment: dict[str, str]) -> Path:
    """Where the engine keeps one session's packets: beside Git's objects, outside every lane.

    Removing a lane worktree never touches the common directory, so packets
    outlive the lane they describe.
    """
    common = Path(_run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", environment=environment).strip())
    return common / "lane-evidence" / "v1" / agent_instance_id


def _published_packets(store: Path) -> list[str]:
    """Published packet directories only; unpublished staging is named apart."""
    return sorted(path.name for path in store.iterdir() if not path.name.startswith(".staging-")) if store.is_dir() else []


def _packet_of(result: Any) -> Any:
    """A result's packet, or None, so a refusal fails an assertion instead of raising."""
    return getattr(result, "packet", None)


def _predecessor(result: Any) -> str | None:
    """The packet a result's packet supersedes; a refusal reads as a marker, not None.

    None means a packet that begins no lineage, so it must not double as failure.
    """
    packet = _packet_of(result)
    return packet.manifest.provenance.predecessor if packet is not None else "<no packet>"


def test_packet_roundtrip(root: Path, environment: dict[str, str]) -> int:
    """Capture, read and rebuild every kind of lane state from custody alone.

    Red on the reviewed base: no custody engine exists, so the response says so
    and the capture assertion fails; nothing raises on import or arguments.
    Red mutation: read the index as HEAD's tree and the rebuilt lane loses its
    staged entries; ignore leaf kind or mode and capture refuses its own copy
    of the executable and the link.
    """
    repo, lane = _custody_lane(root, environment, "lane-custody")
    before = (_tree_state(lane.path), _index_state(lane, environment), _index_file(lane, environment))
    response = _capture_response(repo, "lane-custody", environment)
    assert response.get("success") is True, "custody capture must succeed"
    engine, context, packet = response["engine"], response["context"], response["result"].packet
    assert (_tree_state(lane.path), _index_state(lane, environment), _index_file(lane, environment)) == before, "capture must leave its source untouched"
    stored = _custody_store(repo, "agi-lane-custody", environment) / packet.packet_id
    assert (stored.is_dir(), stored.resolve().is_relative_to(lane.path.resolve())) == (True, False), stored
    _run_git(repo, "worktree", "remove", "--force", str(lane.path), environment=environment)
    after_loss = engine.read_lane_custody(context, packet.packet_id, environment=environment)
    assert getattr(_packet_of(after_loss), "manifest", None) == packet.manifest, after_loss
    assert _payload(engine, context, packet.packet_id, "notes/deep/data.bin", "worktree", environment) == b"\x00\xff\x10binary\x00"
    _run_git(repo, "worktree", "add", str(lane.path), lane.branch, environment=environment)
    _rebuild_index(engine, context, lane, packet, environment)
    _rebuild_worktree(engine, context, lane, packet, environment)
    assert _tree_state(lane.path) == before[0], "reconstruction must restore exact worktree bytes"
    assert _index_state(lane, environment) == before[1], "reconstruction must restore the exact index"
    rebuilt = engine.snapshot_lane_custody(context, environment=environment)
    assert getattr(rebuilt.snapshot, "fingerprint", None) == packet.manifest.fingerprint == response["snapshot"].fingerprint, rebuilt
    _assert_manifest_shape(packet.manifest)
    return 12


def _race(first: dict[str, Any], environment: dict[str, str]) -> list[Any]:
    """Run two identical captures that both finish staging before either may rename.

    The barrier sits in the rename, so both captures have already found no
    published packet; the winner comes first in the returned pair.
    """
    engine, context = first["engine"], first["context"]
    barrier = threading.Barrier(2, timeout=60)

    def rename(*arguments: Any, **options: Any) -> None:
        barrier.wait()
        os.rename(*arguments, **options)

    request = {
        "expected_fingerprint": first["snapshot"].fingerprint,
        "disposition": engine.DISPOSITION_PRESERVED,
        "rationale": "raced",
        "environment": environment,
        "writer": engine.PacketWriter(rename=rename),
    }
    results: list[Any] = []
    threads = [threading.Thread(target=lambda: results.append(engine.capture_lane_custody(context, **request))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 2, results
    return sorted(results, key=lambda result: result.code != "custody_captured")


def test_capture_idempotent(root: Path, environment: dict[str, str]) -> int:
    """An identical request returns its one packet, and racing requests publish it once.

    The loser of the race finds the winner's packet at the rename, reverifies
    it and leaves only its own unpublished staging behind.
    """
    repo, _ = _custody_lane(root, environment, "lane-idem")
    first = _capture_response(repo, "lane-idem", environment)
    again = _capture_response(repo, "lane-idem", environment)
    first_id = first["result"].packet.packet_id
    assert (first["code"], again["code"], _packet_of(again["result"]).packet_id) == ("custody_captured", "custody_already_published", first_id), again
    winner, loser = _race(first, environment)
    store = _custody_store(repo, "agi-lane-idem", environment)
    outcome = (winner.code, loser.code, loser.packet.packet_id, _published_packets(store), (store.parents[2] / loser.staging).is_dir())
    assert outcome == ("custody_captured", "custody_already_published", winner.packet.packet_id, sorted({first_id, winner.packet.packet_id}), True), outcome
    return 4


def test_successor_keeps_original(root: Path, environment: dict[str, str]) -> int:
    """A correction is a new packet naming its predecessor; the original stays readable and unchanged.

    A correction needs no worktree, so it still works once the lane is gone.
    A re-capture may name a verified predecessor; omitting one starts no
    lineage, and a successor without a reason is refused.
    """
    repo, lane = _custody_lane(root, environment, "lane-successor")
    first = _capture_response(repo, "lane-successor", environment)
    engine, context, original = first["engine"], first["context"], first["result"].packet
    manifest_file = _custody_store(repo, context.agent_instance_id, environment) / original.packet_id / "manifest.json"
    original_bytes = manifest_file.read_bytes()
    recaptured = _capture_response(repo, "lane-successor", environment, predecessor=original.packet_id, rationale="recaptured while the lane exists")
    fresh = _capture_response(repo, "lane-successor", environment, rationale="a new lineage")
    missing = _capture_response(repo, "lane-successor", environment, predecessor="0" * 64, rationale="no such packet")
    lineage = (_predecessor(recaptured["result"]), _predecessor(fresh["result"]), missing["code"])
    assert lineage == (original.packet_id, None, "custody_packet_missing"), lineage
    _run_git(repo, "worktree", "remove", "--force", str(lane.path), environment=environment)
    superseded = engine.DISPOSITION_SUPERSEDED
    unexplained = engine.correct_lane_custody(context, original.packet_id, disposition=superseded, rationale=" ", environment=environment)
    corrected = engine.correct_lane_custody(context, original.packet_id, disposition=superseded, rationale="landed by another lane", environment=environment)
    assert (unexplained.code, corrected.code) == ("custody_successor_rationale_required", "custody_captured"), (unexplained, corrected)
    successor, prior = corrected.packet.manifest, original.manifest
    kept = (successor.provenance.disposition, successor.provenance.predecessor, successor.index, successor.worktree, successor.fingerprint)
    assert kept == (superseded, original.packet_id, prior.index, prior.worktree, prior.fingerprint), kept
    reread = engine.read_lane_custody(context, original.packet_id, environment=environment)
    assert (_predecessor(reread), manifest_file.read_bytes()) == (None, original_bytes), reread
    return 5


def test_stale_or_changing_lane_refused(root: Path, environment: dict[str, str]) -> int:
    """No packet holds a state its caller did not consent to, or one that moved mid-capture.

    Red mutation: drop the second full inventory and the leaf that arrived
    while payloads were flushed no longer stops publication.
    """
    repo, lane = _custody_lane(root, environment, "lane-stale")
    engine = _engine()
    assert engine is not None, "custody engine must exist"
    late = lane.path / "late.txt"

    def fsync(descriptor: int) -> None:
        if not late.exists():
            late.write_bytes(b"arrived mid-capture\n")
        os.fsync(descriptor)

    stale = _capture_response(repo, "lane-stale", environment, expected_fingerprint="0" * 64)
    changed = _capture_response(repo, "lane-stale", environment, writer=engine.PacketWriter(fsync=fsync))
    published = _published_packets(_custody_store(repo, "agi-lane-stale", environment))
    outcome = (stale["code"], changed["code"], bool(changed["result"].staging), published, late.read_bytes())
    assert outcome == ("custody_fingerprint_stale", "custody_inventory_changed", True, [], b"arrived mid-capture\n"), outcome
    return 5


def test_injected_failures(root: Path, environment: dict[str, str]) -> int:
    """A failed copy, flush or publication leaves no usable packet and an untouched source.

    Red mutation: stage straight into the digest's own directory and a failed
    flush leaves a partial packet under the name a reader would trust.
    """
    repo, lane = _custody_lane(root, environment, "lane-failure")
    engine = _engine()
    assert engine is not None, "custody engine must exist"
    store = _custody_store(repo, "agi-lane-failure", environment)
    before = (_tree_state(lane.path), _index_file(lane, environment))
    for step in ("write", "fsync", "rename"):

        def fail(*arguments: Any, step: str = step, **options: Any) -> Any:
            raise OSError(errno.EIO, f"injected {step} failure")

        response = _capture_response(repo, "lane-failure", environment, writer=engine.PacketWriter(**{step: fail}))
        result = response["result"]
        staging = store.parents[2] / result.staging
        outcome = (response["code"], result.packet, _published_packets(store), staging.name.startswith(".staging-"), staging.is_dir(), (_tree_state(lane.path), _index_file(lane, environment)) == before)
        assert outcome == ("custody_publication_failed", None, [], True, True, True), (step, outcome)
    return 18


def _restore(path: Path, data: bytes | None) -> None:
    """Put one stored file back exactly as a read-only regular file, or remove an added one."""
    path.unlink(missing_ok=True)
    if data is not None:
        path.write_bytes(data)
        path.chmod(0o444)


def test_corrupt_packet_refused(root: Path, environment: dict[str, str]) -> int:
    """Changed, missing, retyped or extra bytes and a malformed id never verify; a restored packet does.

    Red mutation: compare a payload's mode but not its digests and the altered
    payload verifies.
    """
    repo, _ = _custody_lane(root, environment, "lane-corrupt")
    first = _capture_response(repo, "lane-corrupt", environment)
    engine, context, packet = first["engine"], first["context"], first["result"].packet
    packet_dir = _custody_store(repo, context.agent_instance_id, environment) / packet.packet_id
    digest = next(record.sha256 for record in packet.manifest.worktree if record.path == "notes/deep/data.bin")
    payload, manifest, extra, twin = packet_dir / "payload" / digest, packet_dir / "manifest.json", packet_dir / "extra", root / "identical-bytes"
    twin.write_bytes(payload.read_bytes())
    cases: tuple[tuple[str, Path, Any], ...] = (
        ("altered", payload, lambda: (payload.chmod(0o644), payload.write_bytes(b"\x00\xff\x10altered\x00"))),
        ("missing", payload, payload.unlink),
        ("executable", payload, lambda: payload.chmod(0o755)),
        ("retyped", payload, lambda: (payload.unlink(), payload.symlink_to(twin))),
        ("manifest", manifest, lambda: (manifest.chmod(0o644), manifest.write_bytes(manifest.read_bytes() + b" "))),
        ("extra", extra, lambda: extra.write_bytes(b"unexpected")),
    )
    codes: dict[str, str] = {}
    for name, target, corrupt in cases:
        original = target.read_bytes() if target.exists() else None
        corrupt()
        codes[name] = engine.read_lane_custody(context, packet.packet_id, environment=environment).code
        _restore(target, original)
    malformed = ("../../../x", packet.packet_id.upper(), "0" * 64, packet.packet_id)
    codes |= {name: engine.read_lane_custody(context, name, environment=environment).code for name in malformed}
    expected = dict.fromkeys((name for name, _, _ in cases), "custody_packet_corrupt") | dict(zip(malformed, ("custody_packet_id_invalid", "custody_packet_id_invalid", "custody_packet_missing", "custody_verified"), strict=True))
    assert codes == expected, codes
    return 10 + _assert_foreign_binding_refused(engine, repo, context, packet, environment)


def _assert_foreign_binding_refused(engine: ModuleType, repo: Path, context: Any, packet: Any, environment: dict[str, str]) -> int:
    """Another repository id, or the same bytes under another session, is not this packet.

    Red mutation: skip the binding comparison and both reads verify.
    """
    foreign_repo = _custody_context(engine, repo, "lane-corrupt", assignment=_assignment(repository_id="rep_other"))
    other = _custody_context(engine, repo, "lane-other")
    source = _custody_store(repo, context.agent_instance_id, environment) / packet.packet_id
    shutil.copytree(source, _custody_store(repo, other.agent_instance_id, environment) / packet.packet_id)
    codes = tuple(engine.read_lane_custody(reader, packet.packet_id, environment=environment).code for reader in (foreign_repo, other))
    assert codes == ("custody_packet_foreign", "custody_packet_foreign"), codes
    return 2


def test_protected_leaves_incomplete(root: Path, environment: dict[str, str]) -> int:
    """Special files, unreadable paths and nested repositories are named, never opened, never dropped.

    An ``os.open`` spy proves the pipe, the nested repository's sentinel and
    its administration were examined but never opened, and the capture
    publishes nothing while each leaf stays in place.
    """
    repo, lane = _lane_at_base(root, environment, "lane-protected")
    nested = lane.path / "vendor" / "nested"
    nested.mkdir(parents=True)
    _run_git(nested, "init", "--quiet", environment=environment)
    (nested / "sentinel.txt").write_bytes(b"another repository's bytes\n")
    os.mkfifo(lane.path / "pipe")
    locked = lane.path / "locked.txt"
    locked.write_bytes(b"unreadable\n")
    locked.chmod(0o000)
    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        opened.append(Path(os.fsdecode(path)).name)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    os.open = spy
    try:
        response = _capture_response(repo, "lane-protected", environment)
    finally:
        os.open = real_open
        locked.chmod(0o644)
    expected = {("locked.txt", "protected_inaccessible"), ("pipe", "protected_nonregular"), ("vendor/nested/", "protected_nested_repository")}
    published = _published_packets(_custody_store(repo, "agi-lane-protected", environment))
    outcome = (response["code"], set(response["result"].incomplete), {"pipe", "sentinel.txt", ".git"} & set(opened), published, (nested / "sentinel.txt").exists(), (lane.path / "pipe").exists())
    assert outcome == ("custody_incomplete", expected, set(), [], True, True), outcome
    return 6


def _unmerged(lane: LaneWorktree, environment: dict[str, str]) -> None:
    """Replace one entry with conflict stages 1, 2 and 3, as an unfinished merge leaves it."""
    blobs = [subprocess.run(("git", "-C", str(lane.path), "hash-object", "-w", "--stdin"), input=f"stage {stage}\n".encode(), capture_output=True, env=environment, check=True).stdout.decode().strip() for stage in (1, 2, 3)]
    entries = "".join(f"100644 {blob} {stage}\ttracked.txt\0" for stage, blob in zip((1, 2, 3), blobs, strict=True))
    subprocess.run(("git", "-C", str(lane.path), "update-index", "-z", "--index-info"), input=f"0 {'0' * 40}\ttracked.txt\0{entries}".encode(), env=environment, check=True)


def _intent_to_add(lane: LaneWorktree, environment: dict[str, str]) -> None:
    """Record only the intent to add a file, which Git lists as an ordinary empty blob.

    Flattening it to stage 0 would turn "not yet added" into "added empty".
    """
    (lane.path / "planned.txt").write_text("planned\n", encoding="utf-8")
    _run_git(lane.path, "add", "--intent-to-add", "planned.txt", environment=environment)


def test_index_states_named(root: Path, environment: dict[str, str]) -> int:
    """Each index state a packet cannot rebuild is named by itself and leaves the lane exactly as it was.

    Unmerged stages, intent-to-add, skip-worktree and assume-unchanged each get
    their own fixture and their own code; none is flattened into stage 0.
    """
    cases: tuple[tuple[str, Any, tuple[str, str]], ...] = (
        ("unmerged", _unmerged, ("tracked.txt", "unsupported_index_unmerged")),
        ("ita", _intent_to_add, ("planned.txt", "unsupported_index_intent_to_add")),
        ("skip", lambda lane, env: _run_git(lane.path, "update-index", "--skip-worktree", "tracked.txt", environment=env), ("tracked.txt", "unsupported_index_skip_worktree")),
        ("assume", lambda lane, env: _run_git(lane.path, "update-index", "--assume-unchanged", "tracked.txt", environment=env), ("tracked.txt", "unsupported_index_assume_unchanged")),
    )
    for name, build, named in cases:
        repo, lane = _lane_at_base(root / name, environment, f"lane-index-{name}")
        build(lane, environment)
        before = (_tree_state(lane.path), _index_state(lane, environment), _index_file(lane, environment))
        response = _capture_response(repo, f"lane-index-{name}", environment)
        after = (_tree_state(lane.path), _index_state(lane, environment), _index_file(lane, environment))
        published = _published_packets(_custody_store(repo, f"agi-lane-index-{name}", environment))
        assert (response["code"], response["result"].incomplete, published, after == before) == ("custody_incomplete", (named,), [], True), (name, response)
    return 4 * len(cases)


def test_symlink_escape_refused(root: Path, environment: dict[str, str]) -> int:
    """A link is held as its text, never followed; a linked custody root is refused.

    A linked directory and a linked file both point outside the lane: neither
    target is read, and nothing is written through a linked store.
    """
    repo, lane = _lane_at_base(root / "links", environment, "lane-links")
    outside, elsewhere = root / "outside", root / "elsewhere"
    outside.mkdir()
    elsewhere.mkdir()
    (outside / "secret.txt").write_bytes(b"bytes outside the lane\n")
    (lane.path / "escape").symlink_to(outside, target_is_directory=True)
    (lane.path / "secret-link").symlink_to(outside / "secret.txt")
    response = _capture_response(repo, "lane-links", environment)
    engine, context, packet = response["engine"], response["context"], response["result"].packet
    stored = [path.read_bytes() for path in (_custody_store(repo, context.agent_instance_id, environment) / packet.packet_id / "payload").iterdir()]
    kinds = {record.path: record.kind for record in packet.manifest.worktree}
    escape = _payload(engine, context, packet.packet_id, "escape", "worktree", environment)
    held = (kinds, escape, any(b"outside the lane" in data for data in stored))
    assert held == ({"escape": "symlink", "secret-link": "symlink"}, os.fsencode(outside), False), held
    linked_repo, _ = _lane_at_base(root / "linked-root", environment, "lane-linked-root")
    _custody_store(linked_repo, "unused", environment).parents[1].symlink_to(elsewhere, target_is_directory=True)
    refused = _capture_response(linked_repo, "lane-linked-root", environment)
    assert (refused["code"], list(elsewhere.iterdir())) == ("custody_store_unsafe", []), refused
    return 5


def test_packet_admission(root: Path, environment: dict[str, str]) -> int:
    """A pinned packet decides even where local custody would pass, and only for the lane it binds.

    Red mutation: fall back to local custody when the packet refuses and the
    clean lane is removed.  Sweeps pass no custody, so no packet reaches them.
    """
    from agent_messaging_plugin.lane_worktree_evidence import PacketCustody, PacketVerdict, RetirementCustodyContext

    _, clean = _lane_at_base(root / "clean", environment, "lane-packet-clean")
    repo, unique = _custody_lane(root / "unique", environment, "lane-packet-unique")
    calls: list[str] = []

    def pinned(lane: LaneWorktree, verdict: Any, bound: Path | None = None) -> RetirementCustodyContext:
        packet = PacketCustody("a" * 64, "b" * 64, bound or lane.path, lambda: calls.append(lane.path.name) or verdict)
        return RetirementCustodyContext(f"agi-{lane.path.name}", None, None, packet=packet)

    refused, holds = PacketVerdict(False, "custody_lane_changed", "fixture"), PacketVerdict(True, "custody_packet_holds_lane")
    clean_verdict = lane_worktree_disposability(clean, environment=environment, custody=pinned(clean, refused))
    assert (clean_verdict.disposable, "custody_lane_changed" in clean_verdict.reason) == (False, True), clean_verdict
    unique_verdict = lane_worktree_disposability(unique, environment=environment, custody=pinned(unique, holds))
    assert unique_verdict.disposable and "notes/deep/data.bin" in dict(unique_verdict.custody)["verified_packet"], unique_verdict
    mismatch = lane_worktree_disposability(unique, environment=environment, custody=pinned(unique, holds, clean.path))
    assert not mismatch.disposable and "custody_packet_target_mismatch" in mismatch.reason, mismatch
    try:
        remove_lane_worktree(clean, environment=environment, custody=pinned(clean, refused))
    except LaneWorktreeError:
        pass
    else:
        raise AssertionError("a refusing packet must keep a lane local custody would remove")
    sweep = sweep_orphaned_lane_worktrees(repo, terminal_paths=(unique.path,), environment=environment)
    assert (clean.path.exists(), unique.path.exists(), len(sweep.skipped)) == (True, True, 1), sweep
    assert calls == [clean.path.name, unique.path.name, clean.path.name], f"each predicate reads the packet once, never for another lane: {calls}"
    return 6


def _assert_path_refusals(root: Path) -> int:
    repo = root / "not-a-repo"
    repo.mkdir(parents=True)
    try:
        lane_worktree_for(repo, role_name="../../escape", agent_instance_id="agi-ok")
    except LaneWorktreeError:
        return 1
    raise AssertionError("unsafe role name must fail before any Git command")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lane-worktrees-") as temporary:
        root = Path(temporary).resolve()
        environment = _environment(root)
        checks = _assert_red_pre_fix_shape(root / "red", environment)
        checks += _assert_shared_dirt_isolation(root / "green", environment)
        checks += _assert_sweep_is_exact(root / "sweep", environment)
        checks += _assert_lock_refusal_preserves_shared_venv(root / "locked", environment)
        checks += _assert_foreign_venv_refuses_before_git_removal(
            root / "foreign-venv",
            environment,
        )
        checks += _assert_sweep_tolerates_a_stuck_orphan(root / "stuck", environment)
        checks += _assert_disposability_predicate(root / "disposability", environment)
        checks += _assert_build_output_allowlist(root / "build-output", environment)
        checks += test_empty_profile_scaffold_is_disposable(root / "empty-profile", environment)
        checks += test_ahead_branch_survives_removal(root / "ahead", environment)
        checks += _assert_custody_classes_are_named(root / "classes", environment)
        checks += _assert_path_refusals(root / "refusal")
        checks += _assert_master_base_with_feature_checked_out(root / "master-base", environment)
        checks += _assert_missing_master_uses_head_with_warning(
            root / "missing-master",
            environment,
        )
        checks += test_packet_roundtrip(root / "custody-roundtrip", environment)
        checks += test_injected_failures(root / "custody-failure", environment)
        checks += test_stale_or_changing_lane_refused(root / "custody-stale", environment)
        checks += test_capture_idempotent(root / "custody-idempotent", environment)
        checks += test_successor_keeps_original(root / "custody-successor", environment)
        checks += test_corrupt_packet_refused(root / "custody-corrupt", environment)
        checks += test_protected_leaves_incomplete(root / "custody-protected", environment)
        checks += test_index_states_named(root / "custody-index", environment)
        checks += test_symlink_escape_refused(root / "custody-links", environment)
        checks += test_packet_admission(root / "custody-admission", environment)
    print(f"lane_worktrees_smoke OK: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
