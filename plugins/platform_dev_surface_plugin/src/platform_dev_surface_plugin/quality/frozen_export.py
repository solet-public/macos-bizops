"""A throwaway checkout of one frozen commit, so a landing cannot change what a long run executes.

A scheduled full battery takes 30 to 90 minutes and freezes a commit first. The
live checkout moves whenever something lands, so a run that executes there and
re-verifies it against the frozen commit is interrupted by the next landing.
The run executes in an export instead: a local clone of the live checkout,
detached at the frozen commit, whose bytes are the commit's by construction.

The live checkout's git is only read (a clone reads its objects); git is
written only inside the export. Exports live in one folder per campaign store,
under the system temporary directory and outside the host profile directories
the census smokes protect, in a directory unique to the attempt, and are removed
when the attempt ends. A worker that is killed cannot remove its own, so the
next worker sweeps them: the campaign store admits exactly one worker, so any
export found in that store's folder when a pass starts belongs to a dead
predecessor.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from quality_gates.git_environment import scrubbed_git_environment


def exports_root_for(store_root: Path) -> Path:
    """The folder that holds one campaign store's exports, outside the host profile directories.

    One folder per store, because ``sweep_exports`` is safe only while one store
    admits one worker: everything in a store's folder belongs to a dead
    predecessor. The battery runs smokes that build producers on their own
    temporary stores, and one folder shared by every store would let such a
    smoke delete the running battery's own export. Nothing is created here.
    """
    return Path(tempfile.gettempdir()).resolve() / "full-battery-exports" / hashlib.sha256(str(store_root.resolve()).encode()).hexdigest()[:16]


def _git(*arguments: str) -> None:
    """One routing-scrubbed git call that fails loud with its command and stderr."""
    command = ("git", *arguments)
    done = subprocess.run(command, capture_output=True, text=True, env=scrubbed_git_environment(), check=False)
    if done.returncode:
        raise RuntimeError(f"{' '.join(command)} failed with exit {done.returncode}: {done.stderr.strip()}")


@contextmanager
def frozen_export(root: Path, commit: str, exports_root: Path, label: str) -> Generator[Path]:
    """Yield a detached clone of ``root`` at ``commit`` in ``exports_root``; remove it when the attempt ends.

    The clone gets the live root's ``.venv`` as a symlink: the runner requires
    ``<its repository root>/.venv/bin/python3`` and builds its import path from
    its own tree's source roots ahead of the editable installs, so the code a
    run imports is the export's.
    """
    venv = root / ".venv"
    if not venv.is_dir():
        raise FileNotFoundError(f"the live checkout has no .venv to link into the export: {venv}")
    exports_root.mkdir(parents=True, exist_ok=True)
    export = exports_root / label
    export.mkdir()
    try:
        _git("clone", "--local", "--no-checkout", "--quiet", "--", str(root), str(export))
        _git("-C", str(export), "checkout", "--quiet", "--detach", commit)
        (export / ".venv").symlink_to(venv)
        yield export
    finally:
        shutil.rmtree(export)


def sweep_exports(exports_root: Path) -> None:
    """Remove every export in ``exports_root``; call only while holding the owning store's single-worker exclusion. Creates nothing."""
    if not exports_root.is_dir():
        return
    for leftover in sorted(exports_root.iterdir()):
        if leftover.is_symlink() or not leftover.is_dir():
            leftover.unlink()
        else:
            shutil.rmtree(leftover)
