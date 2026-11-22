#!/usr/bin/env python3
"""Real-fixture smoke for the three read-only GC landing-verification verbs.

The fixture is a real temporary Git worktree with base/master drift, actual
files for SHA-256 hashing, and an external venv symlink.  It deliberately does
not mock filesystem reads or Git subprocesses.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "platform_dev_surface_plugin" / "src"))

from platform_dev_surface_plugin.quality.landing_verify import LandingVerificationError  # noqa: E402
from platform_dev_surface_plugin.quality.operations import QualityOperations  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def _build_git_fixture(root: Path) -> tuple[Path, str]:
    register = root / "quality_gates" / "gate_smokes.txt"
    register.parent.mkdir()
    register.write_text("base_smoke.py\nanchor_smoke.py\n", encoding="utf-8")
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.email", "gc-smoke@example.invalid")
    _git(root, "config", "user.name", "GC smoke")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base_ref = _git(root, "rev-parse", "HEAD").strip()
    register.write_text("master_smoke.py\nanchor_smoke.py\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "master drift")
    _git(root, "checkout", "-q", "-b", "candidate", base_ref)
    register.write_text("base_smoke.py\nanchor_smoke.py\ncandidate_smoke.py\n", encoding="utf-8")
    return register, base_ref


def _test_hash_verify(ops: QualityOperations, root: Path) -> None:
    payload = root / "payload.txt"
    payload.write_text("landing bytes\n", encoding="utf-8")
    external_venv = Path(tempfile.mkdtemp(prefix="gc_external_"))
    try:
        (root / ".venv").symlink_to(external_venv, target_is_directory=True)
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        result = ops.verify_hash_manifest(
            str(root),
            {"payload.txt": digest, "missing.txt": digest},
        )
    finally:
        shutil.rmtree(external_venv, ignore_errors=True)
    statuses = {row["path"]: row["status"] for row in result["files"]}
    _check(statuses == {"missing.txt": "missing", "payload.txt": "exact"}, "hash verb reports real exact + missing files")
    _check(result["all_exact"] is False, "missing file prevents all_exact")
    _check(result["editable_install_shadowing_detected"] is True, "external .venv symlink warns about shadowing")


def _test_merge_predict(ops: QualityOperations, root: Path, base_ref: str, register: Path) -> None:
    before = (_git(root, "status", "--porcelain"), register.read_bytes())
    result = ops.predict_gate_smokes_merge(
        base_ref=base_ref,
        candidate_root_path=str(root),
        current_master="main",
        register_path=str(register.relative_to(root)),
    )
    after = (_git(root, "status", "--porcelain"), register.read_bytes())
    _check(before == after, "merge prediction leaves the candidate directory unchanged")
    _check(result["candidate_root_path"] == str(root.resolve()), "clean report identifies the resolved candidate root")
    _check(result["master_drifted_since_base"] is True, "merge verb detects genuine master drift")
    _check(result["candidate_change_is_append_only"] is True, "merge verb extracts an append-only candidate hunk")
    _check(result["candidate_registrations"] == ["candidate_smoke.py"], "merge verb extracts the candidate registration")
    _check(result["missing_registrations_on_master"] == ["candidate_smoke.py"], "merge verb reports registration missing from master")
    _check((result["merge_verdict"], result["git_merge_file_exit_code"]) == ("clean", 0), "independent edits merge cleanly")
    _check(result["merged_candidate_content"] == "master_smoke.py\nanchor_smoke.py\ncandidate_smoke.py\n", "clean merge contains both edits")


def _test_merge_predict_failures(ops: QualityOperations, root: Path, base_ref: str, register: Path) -> None:
    register.write_text("conflicting_smoke.py\nanchor_smoke.py\n", encoding="utf-8")
    result = ops.predict_gate_smokes_merge(base_ref=base_ref, candidate_root_path=str(root), current_master="main")
    _check(result["candidate_root_path"] == str(root.resolve()), "conflict report identifies the resolved candidate root")
    _check((result["merge_verdict"], result["git_merge_file_exit_code"]) == ("conflict", 1), "overlapping edits produce a conflict report")
    _check("<<<<<<<" in result["merged_candidate_content"], "conflict report includes Git conflict markers")
    try:
        ops.predict_gate_smokes_merge(base_ref=base_ref, candidate_root_path=str(root / "absent"), current_master="main")
    except LandingVerificationError as error:
        _check(str(error).startswith("candidate_root_path must be an existing absolute directory"), "missing candidate root names the public argument")
    else:
        _check(False, "missing candidate root raises LandingVerificationError")


def _test_scope_gaps(ops: QualityOperations) -> None:
    result = ops.detect_scope_regex_gaps(
        [
            "quality_gates/run_smokes.py",
            "plugins/platform_dev_surface_plugin/src/platform_dev_surface_plugin/plugin.py",
            "plugins/default_inference_plugin/research/prompt_engineering/nonexistent_scope_gap_fixture.py",
        ]
    )
    _check("quality_gates/run_smokes.py" in result["in_scope"], "scope verb recognises quality_gates source")
    _check("plugins/platform_dev_surface_plugin/src/platform_dev_surface_plugin/plugin.py" in result["in_scope"], "scope verb recognises plugin source")
    _check(result["out_of_scope"] == ["plugins/default_inference_plugin/research/prompt_engineering/nonexistent_scope_gap_fixture.py"], "scope verb exposes a real known scope gap")
    _check(result["manual_static_analysis_needed"] is True, "scope gap requires manual static analysis")


def main() -> int:
    print("GC landing verification real-fixture smoke")
    fixture = Path(tempfile.mkdtemp(prefix="gc_landing_verify_"))
    try:
        ops = QualityOperations(_REPO_ROOT, "gc-smoke")
        register, base_ref = _build_git_fixture(fixture)
        _test_hash_verify(ops, fixture)
        _test_merge_predict(ops, fixture, base_ref, register)
        _test_merge_predict_failures(ops, fixture, base_ref, register)
        _test_scope_gaps(ops)
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
