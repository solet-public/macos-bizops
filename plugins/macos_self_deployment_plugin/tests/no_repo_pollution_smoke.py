#!/usr/bin/env python3
"""§8.3 no-git-visible-pollution smoke (no pytest).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
§8.3 / §5. Narrowed claim (Architect, Phase-2 review):

  zero ``git status --porcelain`` delta AND any in-tree physical write is
  confined to already-GITIGNORED runtime paths (``profile/data/...``); zero
  writes at the repo ROOT or outside ``.gitignore``.

That still catches the real §5 failure class — a CWD-relative-at-root write
like the historical ``/error.log`` (a managed process whose CWD was a code
tree) — while accepting the intended pre-existing convention that the
production-local ``app_home`` IS ``<repo>/profile`` and ``default_spawn``
writes a per-spawn log under ``<repo>/profile/data/logs/`` (physically
in-tree but gitignored). The earlier version of this smoke used a SYNTHETIC
scratch ``app_home`` and so could not observe that real in-tree write at all.

This exercises the two real writers with PRODUCTION-shaped inputs (a faked
``Popen`` — no real solet launched):

* ``ReleaseManager`` build + cutover → materializes the release OUTSIDE the
  repo (under ``~/.ananta``);
* ``default_spawn`` with ``app_home = <repo>/profile`` → CWD is the
  out-of-tree runtime dir; the per-spawn log lands IN-tree but gitignored.

Asserts: the per-spawn log IS gitignored (``git check-ignore``), its only
``--ignored`` surface is the gitignored runtime dir ``!! profile/data/...``
(never an unignored entry nor one at the repo root), it is confined to
``profile/data/`` (not the root), and ``git status --porcelain`` is
byte-identical WITH the log present AND after cleanup. THROWAWAY proxy.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/no_repo_pollution_smoke.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _release_manager_smoke_support import (  # noqa: E402
    SmokeRecorder,
    build_fake_source,
    make_manager,
    scratch_root,
)
from ananta.core.runtime import get_runtime_dir  # noqa: E402
from macos_self_deployment_plugin import swap_orchestrator  # noqa: E402

_SOLET = "example"
_SPAWN_LOG_REL = Path("profile") / "data" / "logs" / "green_spawn_green_example-green-test.log"


class _FakePopen:
    """Captures the spawn ``cwd`` without launching a real process."""

    last_cwd: str | None = None

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:
        del cmd
        _FakePopen.last_cwd = kwargs.get("cwd")
        self.pid = 4242


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True, text=True, check=False,
    )


def _porcelain() -> str:
    """Tracked/unignored working-tree status (the git-VISIBLE surface)."""
    return _git("status", "--porcelain").stdout


def _is_git_ignored(rel: Path) -> bool:
    """True iff git considers ``rel`` ignored (``git check-ignore`` exits 0)."""
    return _git("check-ignore", str(rel)).returncode == 0


def _ignored_surface(rel: Path) -> str:
    """The ``--ignored`` porcelain entry scoped to ``rel`` (the wholly-ignored
    runtime dir collapses to a single ``!! profile/data/logs/`` line)."""
    return _git("status", "--porcelain", "--ignored", "--", str(rel)).stdout.strip()


def run_smoke() -> int:
    rec = SmokeRecorder()
    print("=== no_repo_pollution_smoke (§8.3: no git-VISIBLE pollution) ===")
    porcelain_before = _porcelain()
    scratch = scratch_root("no-pollution")
    scratch.mkdir(parents=True, exist_ok=True)
    app_home = _REPO_ROOT / "profile"  # PRODUCTION-shaped (not a synthetic scratch)
    spawn_log = _REPO_ROOT / _SPAWN_LOG_REL
    original_popen = subprocess.Popen
    try:
        # ---- Writer 1: ReleaseManager build + cutover (release dir/symlinks/ledger) ----
        source = build_fake_source(scratch)
        releases_root = scratch / "releases"
        mgr = make_manager(source, releases_root)
        candidate = mgr.build_candidate()
        mgr.cutover(candidate)
        rec.check(
            str(_REPO_ROOT) not in str(candidate.release_dir),
            f"release materialized outside the repo tree ({candidate.release_dir})",
        )

        # ---- Writer 2: default_spawn with PRODUCTION app_home = <repo>/profile ----
        subprocess.Popen = _FakePopen  # type: ignore[assignment,misc]
        pid = swap_orchestrator.default_spawn(
            app_home, "green", "example-green-test", _SOLET, candidate,
        )
        subprocess.Popen = original_popen  # type: ignore[misc]
        rec.check(pid == 4242, "default_spawn returned the (faked) child pid")
        rec.check(
            _FakePopen.last_cwd == str(get_runtime_dir(_SOLET))
            and str(_REPO_ROOT) not in (_FakePopen.last_cwd or ""),
            f"default_spawn CWD is the out-of-tree runtime dir (got {_FakePopen.last_cwd!r})",
        )

        # The §5 failure class would write at the repo ROOT (e.g. /error.log).
        # Here the only in-tree write is confined to the gitignored runtime dir.
        rec.check(
            spawn_log.is_file()
            and spawn_log.parent == _REPO_ROOT / "profile" / "data" / "logs",
            f"per-spawn log confined to profile/data/logs, not the repo root "
            f"({_SPAWN_LOG_REL})",
        )
        rec.check(
            _is_git_ignored(_SPAWN_LOG_REL),
            "the in-tree per-spawn log IS gitignored (git check-ignore)",
        )
        rec.check(
            _ignored_surface(_SPAWN_LOG_REL).startswith("!! profile/data/"),
            f"the write's only --ignored surface is a gitignored runtime path "
            f"(!! profile/data/...), never unignored nor at the repo root: "
            f"{_ignored_surface(_SPAWN_LOG_REL)!r}",
        )
        rec.check(
            _porcelain() == porcelain_before,
            "git status --porcelain byte-identical WITH the in-tree log present "
            "(the write is git-invisible)",
        )
    finally:
        subprocess.Popen = original_popen  # type: ignore[misc]
        spawn_log.unlink(missing_ok=True)  # clean up the in-tree test log
        shutil.rmtree(scratch, ignore_errors=True)

    porcelain_after = _porcelain()
    rec.check(
        porcelain_after == porcelain_before,
        "git status --porcelain byte-identical after cleanup — zero git-visible "
        "(tracked/unignored) pollution from the swap path",
    )
    if porcelain_after != porcelain_before:
        before_lines = set(porcelain_before.splitlines())
        for line in porcelain_after.splitlines():
            if line not in before_lines:
                print(f"  NEW: {line}")

    return rec.report("no_repo_pollution_smoke")


if __name__ == "__main__":
    sys.exit(run_smoke())
