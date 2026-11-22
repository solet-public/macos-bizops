#!/usr/bin/env python3
"""Offline smoke controls for the read-only landing-wave helpers."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from quality_gates import landing_wave, landing_wave_battery  # noqa: E402
from quality_gates.candidate_tree import FrozenEntry  # noqa: E402


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _repository() -> tuple[TemporaryDirectory[str], Path]:
    temporary = TemporaryDirectory()
    root = Path(temporary.name).resolve() / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "member-a.txt", "base a\n")
    _write(root, "member-b.txt", "base b\n")
    _git(root, "add", "member-a.txt", "member-b.txt")
    _git(
        root,
        "-c",
        "user.name=Landing Wave Smoke",
        "-c",
        "user.email=landing-wave@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    return temporary, root


def _check_source_and_composition() -> int:
    temporary, root = _repository()
    try:
        base = _git(root, "rev-parse", "HEAD").strip()
        sources = landing_wave.validate_sources(root, (root,), pinned_base=base)
        assert len(sources) == 1 and sources[0].head == base
        candidate = landing_wave.compose_wave(
            root,
            (Path(temporary.name).resolve() / "candidate"),
            (
                FrozenEntry("member-a.txt", b"frozen a\n", "100755"),
                FrozenEntry("member-b.txt", None, None),
                FrozenEntry("new-link", b"member-a.txt", "120000"),
            ),
            base_ref=base,
        )
        evidence = landing_wave.verify_integration_tree(candidate)
        assert evidence.path_count == 2
        assert landing_wave.verify_final_tree(candidate, evidence) == evidence
        assert (candidate.root / "member-a.txt").read_bytes() == b"frozen a\n"
        assert (candidate.root / "new-link").is_symlink()
        (candidate.root / "member-a.txt").write_bytes(b"drifted bytes\n")
        try:
            landing_wave.verify_final_tree(candidate, evidence)
            raise AssertionError("final tree drift was accepted")
        except landing_wave.LandingWaveError as error:
            assert "differs from gated subject" in str(error)
    finally:
        temporary.cleanup()
    return 7


def _check_global_staged_equality() -> int:
    temporary, root = _repository()
    try:
        _write(root, "member-a.txt", "changed a\n")
        _git(root, "add", "member-a.txt")
        evidence = landing_wave.verify_staged_member(
            root,
            member_label="member a / opaque",
            exact_paths=("member-a.txt",),
        )
        assert evidence.paths == ("member-a.txt",)
        assert evidence.member_label == "member a / opaque"
        _write(root, "member-b.txt", "contamination\n")
        _git(root, "add", "member-b.txt")
        try:
            landing_wave.verify_staged_member(
                root,
                member_label="member a / opaque",
                exact_paths=("member-a.txt",),
            )
            raise AssertionError("global staged-index contamination was accepted")
        except landing_wave.LandingWaveError as error:
            assert "does not equal" in str(error)
    finally:
        temporary.cleanup()
    return 3


def _check_member_path_contract() -> int:
    temporary, root = _repository()
    try:
        empty = landing_wave.verify_staged_member(
            root,
            member_label="empty / opaque",
            exact_paths=(),
        )
        assert empty.paths == () and empty.member_label == "empty / opaque"
        _write(root, "member-a.txt", "changed a\n")
        _write(root, "member-b.txt", "changed b\n")
        _git(root, "add", "member-a.txt", "member-b.txt")
        for paths, message in (
            (("member-b.txt", "member-a.txt"), "byte ordered"),
            (("member-a.txt", "member-a.txt"), "duplicate"),
            ((), "does not equal"),
            (("missing.txt",), "does not equal"),
        ):
            try:
                landing_wave.verify_staged_member(
                    root,
                    member_label="arbitrary label",
                    exact_paths=paths,
                )
                raise AssertionError(f"invalid member paths accepted: {paths!r}")
            except landing_wave.LandingWaveError as error:
                assert message in str(error), str(error)
        evidence = landing_wave.verify_staged_member(
            root,
            member_label="complete coverage",
            exact_paths=("member-a.txt", "member-b.txt"),
        )
        assert len(evidence.paths) == 2
    finally:
        temporary.cleanup()
    return 6


def _check_staged_cli() -> int:
    temporary, root = _repository()
    try:
        paths_file = Path(temporary.name) / "paths.txt"
        paths_file.write_text("", encoding="utf-8")
        argv = [
            "verify-staged",
            "--integration-root",
            str(root),
            "--member-label",
            "opaque / cli",
            "--paths-file",
            str(paths_file),
        ]
        output = StringIO()
        with redirect_stdout(output):
            assert landing_wave.main(argv) == 0
        evidence = json.loads(output.getvalue())
        assert evidence["member_label"] == "opaque / cli"
        assert evidence["paths"] == []
        assert len(evidence["staged_sha256"]) == 64
        _write(root, "member-a.txt", "staged\n")
        _git(root, "add", "member-a.txt")
        with redirect_stdout(StringIO()):
            assert landing_wave.main(argv) == 2
        paths_file.write_text("member-a.txt\n", encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            assert landing_wave.main(argv) == 0
        assert json.loads(output.getvalue())["paths"] == ["member-a.txt"]
    finally:
        temporary.cleanup()
    return 7


def _check_cherry_picked_member() -> int:
    temporary, _shared, source_a, _source_b, integration = _two_lane_fixture()
    try:
        _write(source_a, "member-a.txt", "picked bytes\n")
        _git(source_a, "add", "member-a.txt")
        source_commit = _commit(source_a, "source member")
        _git(integration, "cherry-pick", "--no-commit", source_commit)
        evidence = landing_wave.verify_staged_member(
            integration,
            member_label="picked / opaque",
            exact_paths=("member-a.txt",),
        )
        assert evidence.member_label == "picked / opaque"
        assert evidence.paths == ("member-a.txt",)
    finally:
        temporary.cleanup()
    return 2


def _check_one_physical_sweep() -> int:
    temporary, root = _repository()
    try:
        candidate = landing_wave.compose_wave(
            root,
            Path(temporary.name).resolve() / "candidate",
            (FrozenEntry("member-a.txt", b"frozen\n", "100644"),),
            base_ref="HEAD",
        )
        calls: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "ok", "")

        receipt = landing_wave_battery.run_wave_battery(
            candidate.root,
            candidate.manifest,
            (("gate-a",), ("gate-b",)),
            runner=runner,
        )
        assert receipt.physical_sweeps == 1
        assert receipt.command_count == 2
        assert calls == [("gate-a",), ("gate-b",)]
    finally:
        temporary.cleanup()
    return 5


def _commit(root: Path, message: str) -> str:
    _git(
        root,
        "-c",
        "user.name=Landing Wave Smoke",
        "-c",
        "user.email=landing-wave@example.invalid",
        "commit",
        "-qm",
        message,
    )
    return _git(root, "rev-parse", "HEAD").strip()


def _two_lane_fixture() -> tuple[TemporaryDirectory[str], Path, Path, Path, Path]:
    temporary, shared = _repository()
    root = Path(temporary.name).resolve()
    source_a = root / "source-a"
    source_b = root / "source-b"
    integration = root / "integration"
    _git(shared, "worktree", "add", "-q", "-b", "lane/source-a", str(source_a), "HEAD")
    _git(shared, "worktree", "add", "-q", "-b", "lane/source-b", str(source_b), "HEAD")
    _git(shared, "worktree", "add", "-q", "-b", "lane/integration", str(integration), "HEAD")
    return temporary, shared, source_a, source_b, integration


def _stage_and_commit_member(root: Path, path: str, member_label: str) -> str:
    landing_wave.verify_empty_index(root)
    _git(root, "add", path)
    landing_wave.verify_staged_member(root, member_label=member_label, exact_paths=(path,))
    commit = _commit(root, member_label)
    landing_wave.verify_empty_index(root)
    return commit


def _check_two_lane_commit_and_merge() -> int:
    """Approved disposable fixture: two scoped commits then one --no-ff merge."""
    temporary, shared, source_a, source_b, integration = _two_lane_fixture()
    try:
        master_before = _git(shared, "rev-parse", "HEAD").strip()
        _write(source_a, "member-a.txt", "lane a final\n")
        _write(source_b, "member-b.txt", "lane b final\n")
        _write(integration, "member-a.txt", (source_a / "member-a.txt").read_text())
        _write(integration, "member-b.txt", (source_b / "member-b.txt").read_text())
        first = _stage_and_commit_member(integration, "member-a.txt", "member a / opaque")
        second = _stage_and_commit_member(integration, "member-b.txt", "member b / opaque")
        assert _git(shared, "rev-parse", "HEAD").strip() == master_before
        _git(shared, "merge", "--no-ff", "lane/integration", "-m", "merge wave")
        assert _git(shared, "merge-base", "--is-ancestor", first, "HEAD") == ""
        assert _git(shared, "merge-base", "--is-ancestor", second, "HEAD") == ""
    finally:
        temporary.cleanup()
    return 8


def _check_commit_two_failure_preserves_master() -> int:
    """A second-commit hook failure leaves the first commit on integration only."""
    temporary, shared, _source_a, _source_b, integration = _two_lane_fixture()
    try:
        master_before = _git(shared, "rev-parse", "HEAD").strip()
        _write(integration, "member-a.txt", "first member\n")
        first = _stage_and_commit_member(integration, "member-a.txt", "member a / opaque")
        hook = integration / "test-hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        _write(integration, "member-b.txt", "second member\n")
        _git(integration, "add", "member-b.txt")
        landing_wave.verify_staged_member(
            integration,
            member_label="member b / opaque",
            exact_paths=("member-b.txt",),
        )
        failed = subprocess.run(
            (
                "git",
                "-c",
                "user.name=Landing Wave Smoke",
                "-c",
                "user.email=landing-wave@example.invalid",
                "-c",
                f"core.hooksPath={hook.parent}",
                "commit",
                "-m",
                "member b / opaque",
            ),
            cwd=integration,
            text=True,
            capture_output=True,
            check=False,
        )
        assert failed.returncode != 0
        assert _git(shared, "rev-parse", "HEAD").strip() == master_before
        ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor", first, "HEAD"),
            cwd=shared,
            check=False,
        )
        assert ancestor.returncode != 0
        assert _git(integration, "rev-parse", "HEAD").strip() == first
    finally:
        temporary.cleanup()
    return 7


def main() -> int:
    checks = _check_source_and_composition()
    checks += _check_global_staged_equality()
    checks += _check_one_physical_sweep()
    checks += _check_staged_cli()
    checks += _check_member_path_contract()
    checks += _check_cherry_picked_member()
    checks += _check_two_lane_commit_and_merge()
    checks += _check_commit_two_failure_preserves_master()
    print(f"landing_wave_smoke OK: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
