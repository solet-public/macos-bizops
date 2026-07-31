"""Shared helpers for the ``ReleaseManager`` standalone smokes.

These smokes drive the real ``ReleaseManager`` against a **synthetic**
source tree under ``~/.ananta`` scratch (NEVER ``/tmp`` — operator hard
rule) so they exercise the actual ``cp -c`` clone + ``.pth`` re-point +
ledger/symlink code paths without cloning the real 1.8 GB ``.venv`` or
touching the operator's real releases directory.

The synthetic tree mirrors the repo shape the production builder
snapshots: ``ananta/`` + ``plugins/`` first-party code and a ``.venv``
carrying plain-path ``__editable__*.pth`` files (optionally including a
deliberately-stale one whose target dir is absent, for the §8.6 check).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_SRC = REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from macos_self_deployment_plugin.release_manager import ReleaseManager  # noqa: E402

_FIXED_MOMENT = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)
STALE_PLUGIN_NAME = "deleted_plugin"


class SmokeRecorder:
    """Collects PASS/FAIL assertions for a single smoke run (no globals)."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}")

    def report(self, title: str) -> int:
        print(f"\n{title}: {self.passed} passed, {len(self.failed)} failed")
        for label in self.failed:
            print(f"  FAILED: {label}")
        return 1 if self.failed else 0


def scratch_root(tag: str) -> Path:
    """A unique throwaway scratch dir under ``~/.ananta`` (NEVER ``/tmp``)."""
    base = Path("~/.ananta/releases").expanduser()
    return base / f"relmgr-smoke-{tag}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def build_fake_source(
    root: Path, *, big_file_bytes: int = 0, include_stale_pth: bool = False
) -> Path:
    """Create a synthetic source tree under ``root``; return its source root.

    Layout::

        <root>/src_tree/
          ananta/src/ananta/__init__.py
          plugins/foo_plugin/src/foo_plugin/__init__.py
          .venv/bin/python3                         (stub)
          .venv/pyvenv.cfg
          .venv/lib/python3.13/site-packages/__editable__.ananta-2.0.0.pth
          .venv/lib/python3.13/site-packages/__editable__.foo_plugin-1.0.0.pth
          [.venv/.../__editable__.deleted_plugin-1.0.0.pth]   (stale target)
          [.venv/.../big_lib.bin]                             (CoW dedup probe)
    """
    source = root / "src_tree"
    ananta_pkg = source / "ananta" / "src" / "ananta"
    ananta_pkg.mkdir(parents=True)
    (ananta_pkg / "__init__.py").write_text("# synthetic ananta marker\n")
    foo_pkg = source / "plugins" / "foo_plugin" / "src" / "foo_plugin"
    foo_pkg.mkdir(parents=True)
    (foo_pkg / "__init__.py").write_text("# synthetic foo_plugin marker\n")

    venv = source / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("#!/bin/sh\n# stub interpreter\n")
    (venv / "pyvenv.cfg").write_text("home = /opt/homebrew/opt/python@3.13/bin\n")
    site = venv / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (site / "__editable__.ananta-2.0.0.pth").write_text(f"{ananta_pkg.parent}\n")
    (site / "__editable__.foo_plugin-1.0.0.pth").write_text(f"{foo_pkg.parent}\n")
    if include_stale_pth:
        stale_target = source / "plugins" / STALE_PLUGIN_NAME / "src"
        (site / f"__editable__.{STALE_PLUGIN_NAME}-1.0.0.pth").write_text(
            f"{stale_target}\n"
        )
    if big_file_bytes > 0:
        (site / "big_lib.bin").write_bytes(os.urandom(big_file_bytes))
    return source


def make_counting_sha() -> Callable[[Path], str]:
    """A git-sha resolver returning a distinct value per call.

    Keeps release ids unique across builds in one smoke without relying
    on wall-clock second-resolution (a fixed clock + counting sha is
    deterministic and collision-free).
    """
    counter = {"n": 0}

    def resolver(_: Path) -> str:
        value = f"sha{counter['n']:03d}"
        counter["n"] += 1
        return value

    return resolver


def make_manager(
    source_root: Path,
    releases_root: Path,
    *,
    strict_pth: bool = False,
    mid_swap_hook: Callable[[], None] | None = None,
    ledger_write_hook: Callable[[str], None] | None = None,
    keep_releases: int = 3,
) -> ReleaseManager:
    """Build a ReleaseManager wired with deterministic clock + sha."""
    return ReleaseManager(
        homunculus_name="smoke",
        source_root=source_root,
        releases_root=releases_root,
        keep_releases=keep_releases,
        clock=lambda: _FIXED_MOMENT,
        git_sha_resolver=make_counting_sha(),
        strict_pth_validation=strict_pth,
        mid_swap_hook=mid_swap_hook,
        ledger_write_hook=ledger_write_hook,
    )


def free_bytes(path: Path) -> int:
    """Filesystem free space in bytes (``df``-equivalent, NOT ``du``)."""
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def du_bytes(path: Path) -> int:
    """Logical tree size in bytes via ``du -sk`` (double-counts CoW extents)."""
    result = subprocess.run(
        ["du", "-sk", str(path)], capture_output=True, text=True, check=True
    )
    return int(result.stdout.split()[0]) * 1024
