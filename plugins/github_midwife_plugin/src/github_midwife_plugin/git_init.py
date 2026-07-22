"""Workstream A — git-init the born homunculus worktree at genesis.

At genesis a seed-born tree is a plain source directory: the seed never ships
``.git`` (the "no contaminated history travels" invariant — the whole reason
``.git/`` is ``never_copy`` in ``seed_manifest.yaml``), so a freshly-born
homunculus is NOT a git worktree and ``platform_dev_surface_plugin``'s readiness
probe (``git rev-parse --is-inside-work-tree``) fail-louds. This module gives the
born tree a FRESH, EMPTY git history of its OWN: a sensible ``.gitignore``, a
LOCAL git identity, and one initial commit — so the tree is a real worktree from
first boot.

Security-preserving: this starts a brand-new empty history in the newborn; it
NEVER ships the source tree's ``.git``. The seed's "no contaminated history travels"
invariant is untouched — that is WHY ``.git`` is never-copied, and this does not
undo it (it starts a clean local one, it does not import the minting box's).

Idempotent: a tree that is already a git worktree (``.git`` present) is left
UNTOUCHED (``status="skipped"``); an existing ``.gitignore`` is PRESERVED (never
clobbered). ``git`` runs against the NEWBORN's own tree at ``clone_root`` (a
directory outside the fleet worktree), never the shared repository.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .constants import GIT_COMMIT_TIMEOUT_S, GIT_QUERY_TIMEOUT_S

Runner = Callable[..., subprocess.CompletedProcess[str]]


class GitInitError(RuntimeError):
    """Raised when git-initializing the born worktree fails."""


# The born homunculus tree's ``.gitignore``. Focused on runtime state + secrets +
# per-launch-regenerated files; source (``ananta/``, ``plugins/``, the newborn's
# own materialized configs) is tracked. Kept deliberately shorter than the origin
# dev-repo ignore list (no audio/media/binary-library families a fresh seed never
# carries) — a starting point the operator can extend.
_GITIGNORE = """\
# Python
__pycache__/
*.py[cod]
*.egg-info/
.eggs/
build/
dist/

# Virtual environments
.venv
**/.venv
venv
ENV

# Caches
.mypy_cache/
.ruff_cache/
.pytest_cache/

# Runtime state + the per-launch regenerated manifest
profile/config/manifest.yaml
profile/data/
profile/credentials/
profile/documents

# Secrets — never commit
profile/config/vault/
**/passphrase
**/*.enc
*.pem
*.key

# Claude Code local settings (project-shared skills/hooks/commands stay tracked)
.claude/*
!.claude/skills/
!.claude/hooks/
!.claude/commands/

# OS / IDE
.DS_Store
Thumbs.db
.idea/
.vscode/
*.swp
*~
"""

_INITIAL_COMMIT_SUBJECT = "Genesis: initial commit of the {name} homunculus worktree"


def _git(clone_root: Path, args: list[str], timeout: int, run: Runner) -> subprocess.CompletedProcess[str]:
    result = run(
        ["git", "-C", str(clone_root), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise GitInitError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()[:500]}"
        )
    return result


def git_init_worktree(clone_root: Path, name: str, *, run: Runner = subprocess.run) -> dict[str, Any]:
    """Give the born tree at ``clone_root`` a fresh, empty git worktree if it does
    not already have one. Returns a phase record dict.

    Idempotent: if ``clone_root/.git`` already exists the tree is already a
    worktree and this is a no-op (``status="skipped"``). Writes ``.gitignore``
    only if absent (never clobbers operator edits). On a fresh init: ``git init``
    (empty history, branch ``main``), a LOCAL git identity (never ``--global`` —
    the operator's own git config is untouched), ``git add -A``, and one initial
    commit. Raises :class:`GitInitError` on any git failure (fail loud).
    """
    if (clone_root / ".git").exists():
        return {"step_name": "git_init", "status": "skipped", "reason": "already a git worktree"}

    gitignore = clone_root / ".gitignore"
    gitignore_written = False
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE, encoding="utf-8")
        gitignore_written = True

    _git(clone_root, ["-c", "init.defaultBranch=main", "init", "-q"], GIT_COMMIT_TIMEOUT_S, run)
    # LOCAL identity only (repo config, NEVER --global): the newborn owns its
    # worktree identity and can change it; the operator's global config is
    # never touched.
    _git(clone_root, ["config", "user.name", name], GIT_QUERY_TIMEOUT_S, run)
    _git(clone_root, ["config", "user.email", f"{name}@localhost"], GIT_QUERY_TIMEOUT_S, run)
    _git(clone_root, ["add", "-A"], GIT_COMMIT_TIMEOUT_S, run)
    _git(
        clone_root,
        ["-c", "commit.gpgsign=false", "commit", "-q", "-m", _INITIAL_COMMIT_SUBJECT.format(name=name)],
        GIT_COMMIT_TIMEOUT_S,
        run,
    )
    return {
        "step_name": "git_init",
        "status": "completed",
        "gitignore_written": gitignore_written,
        "branch": "main",
    }


__all__ = ["GitInitError", "Runner", "git_init_worktree"]
