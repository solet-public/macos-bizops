#!/usr/bin/env python3
"""Read-only composition and tree evidence helpers for a group of members.

The caller owns Git mutations. These helpers measure the composed tree and
each member's staged paths, identified by an opaque caller-supplied label.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from quality_gates.candidate_tree import (
    CandidateTree,
    CandidateTreeError,
    FrozenEntry,
    materialize_frozen_entries,
    validate_candidate_manifest,
)


class LandingWaveError(RuntimeError):
    """A wave is not a safe, equality-bound subject for the Git authority."""


@dataclass(frozen=True)
class WaveSource:
    """Read-only source-worktree measurement retained in wave evidence."""

    root: str
    head: str
    branch: str
    status_sha256: str


@dataclass(frozen=True)
class StagedMemberEvidence:
    """Exact staged paths for one member before its normal hook-enabled commit."""

    member_label: str
    paths: tuple[str, ...]
    staged_sha256: str


@dataclass(frozen=True)
class WaveTreeEvidence:
    """A complete tree identity, including modes and symlink target bytes."""

    manifest_sha256: str
    tree_sha256: str
    path_count: int


def _git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        raise LandingWaveError(f"git {' '.join(arguments)} failed: {detail or completed.returncode}")
    return completed.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_member_path(path: str) -> str:
    """Reject one unsafe or nonportable member path."""
    pure = PurePosixPath(path)
    if not path or "\x00" in path or "\n" in path or "\r" in path:
        raise LandingWaveError(f"unsafe exact path: {path!r}")
    if pure.is_absolute() or path != pure.as_posix():
        raise LandingWaveError(f"unsafe exact path: {path!r}")
    if any(part in {"", ".", "..", ".git"} for part in pure.parts):
        raise LandingWaveError(f"unsafe exact path: {path!r}")
    return path


def _canonical_paths(paths: Sequence[str]) -> tuple[str, ...]:
    normalized = [_validate_member_path(path) for path in paths]
    if len(set(normalized)) != len(normalized):
        raise LandingWaveError("member contains duplicate exact paths")
    ordered = tuple(sorted(normalized, key=lambda item: item.encode("utf-8")))
    if tuple(normalized) != ordered:
        raise LandingWaveError("member exact paths are not UTF-8 byte ordered")
    return ordered


def validate_sources(
    repository_root: Path,
    sources: Sequence[Path],
    *,
    pinned_base: str,
) -> tuple[WaveSource, ...]:
    """Measure source roots without accepting their branch history as content.

    Dirty source lanes are expected: their exact final bytes are frozen
    separately.  The measurement instead records their branch/HEAD/status so
    a caller can detect source drift after that freeze.
    """
    root = repository_root.resolve()
    expected_common = _git(root, "rev-parse", "--git-common-dir").strip()
    if not expected_common:
        raise LandingWaveError("repository has no Git common directory")
    if not sources:
        raise LandingWaveError("wave requires at least one source worktree")
    measured: list[WaveSource] = []
    for source in sources:
        candidate = source.resolve()
        if not candidate.is_dir():
            raise LandingWaveError(f"source worktree is not a directory: {candidate}")
        common = _git(candidate, "rev-parse", "--git-common-dir").strip()
        if common != expected_common:
            raise LandingWaveError(f"source is not a worktree of the declared repository: {candidate}")
        if _git(candidate, "merge-base", pinned_base, pinned_base).strip() != pinned_base.encode():
            raise LandingWaveError("pinned base does not resolve consistently in source worktree")
        head = _git(candidate, "rev-parse", "HEAD").decode().strip()
        branch = _git(candidate, "branch", "--show-current").decode().strip()
        if not branch:
            raise LandingWaveError(f"source worktree is detached: {candidate}")
        status = _git(candidate, "status", "--porcelain=v1", "-z")
        measured.append(WaveSource(str(candidate), head, branch, _sha256(status)))
    return tuple(measured)


def compose_wave(
    repository_root: Path,
    destination: Path,
    entries: Sequence[FrozenEntry],
    *,
    base_ref: str,
) -> CandidateTree:
    """Create a candidate from pinned-base plus pre-frozen entries only."""
    try:
        return materialize_frozen_entries(
            repository_root,
            destination,
            entries,
            base_ref=base_ref,
        )
    except CandidateTreeError as exc:
        raise LandingWaveError(f"wave composition refused: {exc}") from exc


def _tree_evidence(root: Path, manifest: Path) -> WaveTreeEvidence:
    paths = validate_candidate_manifest(root, manifest)
    digest = hashlib.sha256()
    for relpath in paths:
        path = root / relpath
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            payload = os.fsencode(os.readlink(path))
        else:
            payload = path.read_bytes()
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{mode:o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    manifest_bytes = manifest.read_bytes()
    return WaveTreeEvidence(_sha256(manifest_bytes), digest.hexdigest(), len(paths))


def verify_integration_tree(candidate: CandidateTree) -> WaveTreeEvidence:
    """Verify the complete frozen composite before the one physical sweep."""
    return _tree_evidence(candidate.root, candidate.manifest)


def verify_staged_member(
    integration_root: Path,
    *,
    member_label: str,
    exact_paths: Sequence[str],
) -> StagedMemberEvidence:
    """Match the whole index to exact paths, including an empty member.

    The label is opaque evidence for the caller; paths alone determine equality.
    """
    expected = _canonical_paths(exact_paths)
    raw = _git(integration_root.resolve(), "diff", "--cached", "--name-only", "--no-renames", "-z")
    actual = tuple(item.decode("utf-8") for item in raw.split(b"\0") if item)
    sorted_actual = tuple(sorted(actual, key=lambda item: item.encode("utf-8")))
    if actual != sorted_actual:
        raise LandingWaveError("staged index is not byte-sorted; refuse ambiguous equality")
    if actual != expected:
        raise LandingWaveError(f"staged index does not equal member {member_label}: expected={expected!r} actual={actual!r}")
    return StagedMemberEvidence(member_label, actual, _sha256(raw))


def verify_empty_index(integration_root: Path) -> None:
    """Prove the pre/post-member integration index has no residual paths."""
    raw = _git(integration_root.resolve(), "diff", "--cached", "--name-only", "--no-renames", "-z")
    if raw:
        raise LandingWaveError("integration index is not empty")


def verify_final_tree(
    candidate: CandidateTree,
    expected: WaveTreeEvidence,
) -> WaveTreeEvidence:
    """Refuse a post-gate tree whose complete content differs from the gate subject."""
    actual = _tree_evidence(candidate.root, candidate.manifest)
    if actual != expected:
        raise LandingWaveError(f"final composite tree differs from gated subject: expected={expected} actual={actual}")
    return actual


def _read_paths(path: Path) -> tuple[str, ...]:
    try:
        return tuple(line for line in path.read_text(encoding="utf-8").splitlines() if line)
    except (OSError, UnicodeError) as exc:
        raise LandingWaveError(f"exact-path file is unreadable: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """Emit typed read-only evidence for controller procedure steps."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    staged = subparsers.add_parser("verify-staged")
    staged.add_argument("--integration-root", type=Path, required=True)
    staged.add_argument("--member-label", required=True)
    staged.add_argument("--paths-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        evidence = verify_staged_member(
            arguments.integration_root,
            member_label=arguments.member_label,
            exact_paths=_read_paths(arguments.paths_file),
        )
        print(json.dumps(asdict(evidence), sort_keys=True))
    except (LandingWaveError, OSError, UnicodeError) as exc:
        print(f"landing_wave REFUSED: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
