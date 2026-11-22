#!/usr/bin/env python3
"""Prove the run_smokes candidate digest hashes source, never runner scratch.

Other smokes create and delete ``workbench/.scratch`` entries inside the real
tree while a battery digests that tree. The digest file set must therefore
exclude them, so a scratch directory that vanishes mid-digest cannot fail it.
"""

from __future__ import annotations

import shutil
import string
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from quality_gates import git_environment, run_smokes  # noqa: E402
from quality_gates.tests import fast_return_census_fixtures  # noqa: E402

smoke_scratch = fast_return_census_fixtures.smoke_scratch
_SCRATCH_PREFIX = "workbench/.scratch/"
_NOTE = Path("workbench", "note.md")  # built from parts: a spelled-out path in source reads as a document citation
_scrubbed_git_environment = git_environment.scrubbed_git_environment  # C4 patches the module attribute


def _git(root: Path, *arguments: str) -> None:
    done = subprocess.run(("git", *arguments), cwd=root, env=_scrubbed_git_environment(), check=False, capture_output=True, text=True)
    assert done.returncode == 0, (arguments, done.stderr)


@contextmanager
def _fixture_repo() -> Generator[Path]:
    """Yield a git repo with committed ``src/a.py`` and an untracked note under ``workbench``.

    The census refuses scratch beneath a protected host directory, and the gate's TMPDIR is
    one. That guard protects the real census from writing there; it has no meaning for a
    throwaway fixture repo, so it is switched off here and the real ``smoke_scratch`` still
    does the writing.
    """
    with tempfile.TemporaryDirectory() as temporary, patch.object(fast_return_census_fixtures, "_check_scratch", lambda _root, _scratch: None):
        root = Path(temporary).resolve()
        _git(root, "init", "-q")
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(root, "add", "src/a.py")
        _git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "fixture")
        (root / "workbench").mkdir()
        (root / _NOTE).write_text("note\n", encoding="utf-8")
        yield root


def _write_device_alias(scratch: Path) -> None:
    """Create the untracked file that failed the landing run."""
    alias = scratch / "device-home" / "device-alias"
    alias.parent.mkdir()
    alias.write_text("alias\n", encoding="utf-8")


def _relative(root: Path, paths: tuple[Path, ...]) -> list[str]:
    return [path.relative_to(root).as_posix() for path in paths]


def _check_listing_excludes_scratch() -> None:
    """C1: the git listing keeps source and untracked notes but drops scratch."""
    with _fixture_repo() as root, smoke_scratch(root) as scratch:
        _write_device_alias(scratch)
        names = _relative(root, run_smokes._candidate_paths_for_digest(root))
        assert "src/a.py" in names and _NOTE.as_posix() in names, names
        assert not [name for name in names if name.startswith(_SCRATCH_PREFIX)], names


def _check_scratch_vanishing_after_listing() -> None:
    """C2: scratch removed right after the listing, as a finishing sibling smoke does."""
    listing = run_smokes._indexed_candidate_paths
    with _fixture_repo() as root, smoke_scratch(root) as scratch:
        _write_device_alias(scratch)

        def list_then_remove_scratch(listed_root: Path, output: bytes) -> tuple[Path, ...]:
            paths = listing(listed_root, output)
            shutil.rmtree(scratch)
            return paths

        with patch.object(run_smokes, "_indexed_candidate_paths", list_then_remove_scratch):
            digest = run_smokes._candidate_digest(root)
        assert len(digest) == 64 and set(digest) <= set(string.hexdigits.lower()), digest


def _check_digest_ignores_scratch_presence() -> None:
    """C3: the digest is the same with scratch present and after it is removed."""
    with _fixture_repo() as root, smoke_scratch(root) as scratch:
        _write_device_alias(scratch)
        with_scratch = run_smokes._candidate_digest(root)
        shutil.rmtree(scratch)
        assert run_smokes._candidate_digest(root) == with_scratch


def _check_git_free_walk_excludes_scratch() -> None:
    """C4: a tree with no git index falls back to a walk that also drops scratch."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "only.txt").write_text("only\n", encoding="utf-8")
        leaf = root / "workbench" / ".scratch" / "x"
        leaf.mkdir(parents=True)
        (leaf / "y").write_text("y\n", encoding="utf-8")

        def fenced_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
            """Keep git from finding a repository above the fixture (a temp dir may sit inside one)."""
            return _scrubbed_git_environment({**(overrides or {}), "GIT_CEILING_DIRECTORIES": str(root.parent)})

        with patch.object(git_environment, "scrubbed_git_environment", fenced_environment):
            paths = run_smokes._candidate_paths_for_digest(root)
        assert paths == (root / "only.txt",), paths


_CHECKS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("C1 listing excludes scratch", _check_listing_excludes_scratch),
    ("C2 scratch vanishing after the listing", _check_scratch_vanishing_after_listing),
    ("C3 digest ignores scratch presence", _check_digest_ignores_scratch_presence),
    ("C4 git-free walk excludes scratch", _check_git_free_walk_excludes_scratch),
)


def main() -> None:
    failures = 0
    for name, check in _CHECKS:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - every check must report, so the red run shows all of them
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    if failures:
        raise SystemExit(1)
    print(f"run-smokes digest scratch smoke: {len(_CHECKS)} checks passed")


if __name__ == "__main__":
    main()
