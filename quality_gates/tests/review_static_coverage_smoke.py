"""Fixture-only candidate coverage, integrity and receipt lifecycle regression.

The first assertion is missing-API evidence at base. All other assertions use
real planner code and temporary native-identity-shaped fixtures, never services.

Fixtures use private temporary Git histories and retained receipt files.
They assert that full no-renames deltas preserve deleted and added paths.
The planner CLI may provision an empty evidence file but never runs a gate.
A sentinel invocation proves that identity argv selects the candidate tree.
Invalid-input fixtures pair refusals with unchanged existing bytes.
This smoke makes no Controller execution or live-system acceptance claim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parents[2]
assert (_ROOT / "quality_gates/review_static_coverage.py").is_file(), "RED_API_PRESENT"
sys.path.insert(0, str(_ROOT))

from quality_gates import review_static_coverage as coverage  # noqa: E402
from quality_gates.code_quality_scope import CLASS_NAMES  # noqa: E402

_IDENTITY_GATE = "quality_gates/macos_" + "bizops_identity_gate.py"
_CANDIDATE_SENTINEL = "COVERAGE_CANDIDATE_IDENTITY_GATE_RAN"
_CAPTURE_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "fish", "cmd", "cmd.exe", "powershell", "pwsh"})


def _git(root: Path, *args: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args], cwd=root, env=env, check=True, capture_output=True)
    return result.stdout


def _write(root: Path, path: str, value: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value)


def _code(index: int) -> str:
    if index == 75:
        return "def complex_fn(a: int) -> int:\n" + "".join(f"    if a == {j}:\n        return {j}\n" for j in range(16)) + "    return 0\n"
    return "def value() -> int:\n    return 1\n"


def _scope_file(root: Path, paths: Sequence[str]) -> Path:
    path = root / "delta.paths"
    path.write_text("".join(item + "\n" for item in paths))
    return path


def _fixture(root: Path) -> tuple[coverage.Subject, Path, Path, Path]:
    # Keep the baseline and candidate commits native so identity checks use real Git.
    repo = root / "repo"
    repo.mkdir()
    base = _fixture_base(repo)
    commit = _fixture_candidate(repo)
    paths = _fixture_paths(repo, base, commit)
    identity = _fixture_identity(root, repo, base, commit, paths)
    snapshot, manifest = _fixture_snapshot(root, repo, commit)
    delta_file = _scope_file(root, paths)
    rename_file = root / "evidence" / "rename-map.txt"
    rename_file.parent.mkdir()
    subject = coverage.load_subject(repo, identity, snapshot, manifest, delta_file, rename_file)
    assert rename_file.is_file() and rename_file.read_bytes() == b"", "IDENTITY_RENAME_PROVISIONED"
    return subject, identity, snapshot, manifest


def _fixture_base(repo: Path) -> str:
    _git(repo, "init", "-q")
    _write(repo, "quality_gates/__init__.py", "")
    _write(repo, _IDENTITY_GATE, f'print("{_CANDIDATE_SENTINEL}")\n')
    _write(repo, "notes.md", "base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return _git(repo, "rev-parse", "HEAD").decode().strip()


def _fixture_candidate(repo: Path) -> str:
    for i in range(76):
        prefix = "ananta/src/fixture" if i < 29 else "solet_cli/tests/fixture"
        _write(repo, f"{prefix}/case_{i:02}.py", _code(i))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "candidate")
    return _git(repo, "rev-parse", "HEAD").decode().strip()


def _fixture_paths(repo: Path, base: str, commit: str) -> list[str]:
    return sorted(path.decode() for path in _git(repo, "diff", "--name-only", "-z", "--no-renames", base, commit).split(b"\0") if path)


def _fixture_identity(root: Path, repo: Path, base: str, commit: str, paths: list[str]) -> Path:
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    entries: list[dict[str, object]] = []
    for path in paths:
        data = (repo / path).read_bytes()
        entries.append({"path": path, "base": "absent", "final": {"mode": 0o100644, "sha256": coverage.digest(data)}})
    patch = _git(repo, "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", base, commit)
    identity = root / "identity.json"
    identity.write_bytes(coverage.canonical({"unit_id": "unt_fixture", "repository_id": "rep_fixture", "base_commit_sha": base, "candidate_commit_sha": commit, "candidate_tree_sha": tree, "target_branch": "master", "patch_path": "candidate.patch", "patch_sha256": coverage.digest(patch), "entries": entries}))
    return identity


def _fixture_snapshot(root: Path, repo: Path, commit: str) -> tuple[Path, Path]:
    snapshot = root / "snapshot"
    snapshot.mkdir()
    all_paths = sorted(p.decode() for p in _git(repo, "ls-tree", "-rz", "--name-only", commit).split(b"\0") if p)
    for path in all_paths:
        _write(snapshot, path, (repo / path).read_text())
    manifest = root / "tree.paths"
    manifest.write_text("".join(path + "\n" for path in all_paths))
    return snapshot, manifest


def _binding(subject: coverage.Subject, catalog: coverage.Catalog) -> dict[str, object]:
    return {"candidate_sha256": subject.candidate_sha256, "catalog_sha256": catalog.sha256, "manifest_sha256": subject.manifest_sha256, "delta_sha256": subject.delta_sha256}


def _claim(root: Path, subject: coverage.Subject, catalog: coverage.Catalog, key: str, selected: tuple[str, ...], *, code: int = 0) -> dict[str, object]:
    selected = tuple(sorted(selected))
    gate = next(gate for gate in catalog.gates if gate.key == key)
    argv: list[str] = []
    for arg in gate.argv:
        argv.extend(selected if arg == "{paths}" else [arg.replace("{repo}", subject.repo_root).replace("{commit}", subject.commit).replace("{delta_file}", subject.delta_file).replace("{rename_file}", subject.rename_file)])
    receipt: dict[str, object] = {**_binding(subject, catalog), "gate": key, "selected": list(selected), "argv": argv, "exit_code": code, "status": "completed", "output": "fixture captured run receipt; no real gate run claimed"}
    evidence = root / f"{key}.receipt.json"
    evidence.write_bytes(coverage.canonical(receipt))
    return {**receipt, "evidence_ref": str(evidence), "evidence_sha256": coverage.digest(evidence.read_bytes())}


def _rows(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(row["gate"]): row for row in cast(list[dict[str, object]], report["rows"])}


def _refused(call: Callable[[], object], label: str) -> None:
    try:
        call()
    except coverage.CoverageError:
        return
    raise AssertionError(label)


def _partial(root: Path, subject: coverage.Subject, catalog: coverage.Catalog) -> None:
    selected = subject.live[:29]
    claim = _claim(root, subject, catalog, "radon_cc", selected)
    report = coverage.report(subject, catalog, [claim])
    row = _rows(report)["radon_cc"]
    assert "solet_cli/tests/fixture/case_75.py" in cast(list[str], row["expected"]), "SOLET_TESTS_INCLUDED"
    assert row["missing"] == list(subject.live[29:]) and len(cast(list[str], row["missing"])) == 47, "MISSING_47"
    assert row["status"] == "PARTIAL" and report["status"] == "PARTIAL", "PARTIAL_NOT_COMPLETE"
    assert _rows(report).get("identity", {}).get("status") == "NOT RUN", "IDENTITY_NOT_RUN"
    assert "complex_fn" in (root / "snapshot/solet_cli/tests/fixture/case_75.py").read_text(), "OMITTED_COMPLEX_FIXTURE"
    assert not row["partial_declared"], "UNDECLARED_PARTIAL"
    claim["partial_reason"] = "bounded diagnostic sample"
    justified = _rows(coverage.report(subject, catalog, [claim]))["radon_cc"]
    assert justified["status"] == "PARTIAL" and justified["partial_declared"], "JUSTIFIED_PARTIAL"
    claim["partial_reason"] = " "
    assert not _rows(coverage.report(subject, catalog, [claim]))["radon_cc"]["partial_declared"], "BLANK_REASON"
    print("PASS MISSING_47 PARTIAL_NOT_COMPLETE IDENTITY_NOT_RUN SOLET_TESTS_INCLUDED")


def _complete(root: Path, subject: coverage.Subject, catalog: coverage.Catalog) -> None:
    claims = [_claim(root, subject, catalog, gate.key, subject.changed if gate.applicability == "candidate" else subject.live) for gate in catalog.gates if gate.audience == "peer"]
    full = coverage.report(subject, catalog, claims)
    assert full["status"] == "FOCUSED-COMPLETE" and full["full_battery_complete"] is False, "FULL_SELECTION"
    assert all(row["status"] == "CONTROLLER-DEFERRED" for row in _rows(full).values() if row["audience"] == "controller"), "DEFERRED_NOT_PASS"
    coverage.validate_report(full, subject, catalog)
    corrected = coverage.report(subject, catalog, claims, supersedes=full)
    assert corrected["supersedes_sha256"] == coverage.digest(coverage.canonical(full)), "SUPERSEDES_READBACK"
    coverage.validate_report(cast(dict[str, object], json.loads(coverage.canonical(corrected))), subject, catalog)
    assert coverage.canonical(full) == coverage.canonical(coverage.report(subject, catalog, claims)), "DETERMINISTIC_READBACK"
    no_claims = coverage.report(subject, catalog)
    other_path = str(Path(subject.rename_file).with_name("different-rename-map.txt").resolve())
    other_subject = replace(subject, rename_file=other_path)
    _refused(
        lambda: coverage.report(other_subject, catalog, supersedes=no_claims),
        "DIFFERENT_RENAME_SUPERSEDES",
    )
    stale = dict(claims[0], candidate_sha256="0" * 64)
    _refused(lambda: coverage.report(subject, catalog, [stale]), "STALE_SUBJECT")
    _refused(lambda: coverage.validate_report(full, replace(subject, candidate_sha256="0" * 64), catalog), "STALE_READBACK")
    _refused(lambda: coverage.report(subject, replace(catalog, sha256="f" * 64), claims), "STALE_CATALOG")
    _refused(lambda: coverage.report(replace(subject, candidate_sha256="0" * 64), catalog, supersedes=full), "STALE_SUPERSEDES")
    print("PASS FULL_SELECTION STALE_SUBJECT DETERMINISTIC_READBACK SUPERSEDES_READBACK")


def _bad_claims(root: Path, subject: coverage.Subject, catalog: coverage.Catalog) -> None:
    good = _claim(root, subject, catalog, "radon_cc", subject.live)
    no_evidence = dict(good)
    no_evidence.pop("evidence_ref")
    _refused(lambda: coverage.report(subject, catalog, [no_evidence]), "MISSING_EVIDENCE_METADATA")
    inaccessible = dict(good, evidence_ref=str(root / "missing.receipt"))
    assert _rows(coverage.report(subject, catalog, [inaccessible]))["radon_cc"]["status"] == "UNMEASURABLE", "UNAVAILABLE_EVIDENCE"
    _refused(lambda: coverage.report(subject, catalog, [dict(good, argv=["true"])]), "WRONG_COMMAND")
    failed = _claim(root, subject, catalog, "radon_cc", subject.live, code=2)
    assert _rows(coverage.report(subject, catalog, [failed]))["radon_cc"]["status"] == "FAIL", "NONZERO_EXIT"
    _refused(lambda: coverage.report(subject, catalog, [dict(good, selected=[subject.live[0]] * 2)]), "DUPLICATE_PATHS")
    _refused(lambda: coverage.report(subject, catalog, [dict(good, selected=["../unsafe.py"])]), "UNSAFE_PATHS")
    extra = _claim(root, subject, catalog, "radon_cc", (*subject.live, "quality_gates/unexpected.py"))
    row = _rows(coverage.report(subject, catalog, [extra]))["radon_cc"]
    assert row["status"] == "PARTIAL" and row["unexpected"] == ["quality_gates/unexpected.py"], "UNEXPECTED_PATH"
    _refused(lambda: coverage.report(subject, catalog, [good, good]), "DUPLICATE_GATE")
    print("PASS MISSING_EVIDENCE NONZERO_EXIT DUPLICATE_PATHS UNSAFE_PATHS UNEXPECTED_PATH")


def _edges(subject: coverage.Subject, catalog: coverage.Catalog) -> None:
    deleted = "solet_cli/tests/old.py"
    new = "solet_cli/tests/new.py"
    rename = replace(subject, changed=(new, deleted), live=(new,), deleted=(deleted,))
    row = _rows(coverage.report(rename, catalog))["radon_cc"]
    assert row["expected"] == [new] and row["excluded"] == [{"path": deleted, "reason": "deleted; file scanner N/A"}], "RENAME_SIDES"
    empty = replace(subject, changed=("notes.md",), live=("notes.md",), deleted=())
    rows = _rows(coverage.report(empty, catalog))
    assert rows["radon_cc"]["status"] == "N/A" and rows["identity"]["status"] == "NOT RUN", "NONPY_IDENTITY"
    deletion = replace(subject, changed=(deleted,), live=(), deleted=(deleted,))
    rows = _rows(coverage.report(deletion, catalog))
    assert rows["radon_cc"]["status"] == "N/A" and rows["identity"]["expected"] == [deleted], "DELETION_NA"
    assert _rows(coverage.report(replace(subject, changed=(), live=()), catalog))["identity"]["status"] == "NOT RUN", "EMPTY_DELTA"
    print("PASS RENAME_SIDES NONPY_IDENTITY DELETION_NA EMPTY_DELTA")


def _catalog(catalog: coverage.Catalog) -> None:
    _catalog_capture(catalog)
    _catalog_keys(catalog)
    _catalog_direct_launches(catalog)
    _catalog_identity_template(catalog)


def _load_catalog() -> coverage.Catalog:
    try:
        return coverage.load_catalog()
    except coverage.CoverageError as error:
        detail = str(error)
        for key in ("lint", "toolchain_preflight"):
            prefix = f"{key}: capture argv may not begin with shell basename"
            if detail.startswith(prefix):
                raise AssertionError(f"CAPTURE_ARGV_ACCEPTED {key}: {detail}") from error
        raise


def _catalog_capture(catalog: coverage.Catalog) -> None:
    for gate in catalog.gates:
        assert _capture_accepts(gate.argv), f"CAPTURE_ARGV_ACCEPTED {gate.key}: {gate.argv!r}"


def _catalog_keys(catalog: coverage.Catalog) -> None:
    keys = {gate.key for gate in catalog.gates}
    assert {"format", "lint", "typing", "god_class", "radon_cc", "radon_mi", "identity", "candidate_tree", "aggregate", "smokes", "sql_access", "dependency_declaration"} <= keys, "CATALOG_REQUIRED_KEYS"
    assert {key.removeprefix("aggregate:") for key in keys if key.startswith("aggregate:")} == set(CLASS_NAMES), "AGGREGATE_CLASS_ENUMERATION"


def _catalog_direct_launches(catalog: coverage.Catalog) -> None:
    lint = next(g for g in catalog.gates if g.key == "lint")
    assert lint.argv == ("quality_gates/ruff_repo.sh", "{paths}"), "CATALOG_LINT_ARG"

    preflight = next(g for g in catalog.gates if g.key == "toolchain_preflight")
    assert preflight.argv == ("deployment/scripts/check_gate_toolchain.sh", "gate"), "CATALOG_PREFLIGHT_ARG"


def _catalog_identity_template(catalog: coverage.Catalog) -> None:
    identity = next(g for g in catalog.gates if g.key == "identity")
    assert identity.audience == "peer", "IDENTITY_PEER"
    assert identity.argv[0] == "python3", "IDENTITY_CAPTURE_PYTHON"
    pairs = tuple(zip(identity.argv, identity.argv[1:], strict=False))
    assert ("--rename-file", "{rename_file}") in pairs, "IDENTITY_RENAME_ARG"
    controller = next(g for g in catalog.gates if g.key == "candidate_tree")
    assert "--rename-file" not in controller.argv, "CONTROLLER_RENAME_FREE"


# Seat-authorized under agm-_58973f533abf4a55a416cde81adf0554. This mirrors
# Project Solet evidence_capture.py:70-84 at f2a64b281278; extraction and
# retirement are tracked separately by iss_32169e6c-f2bb-4afe-9e20-d4fa51823d1b.
def _capture_accepts(argv: tuple[str, ...]) -> bool:
    """Pinned mirror of Project Solet capture_evidence argv acceptance."""
    return bool(argv) and all(token.strip() for token in argv) and Path(argv[0]).name not in _CAPTURE_SHELLS


def _catalog_inputs(root: Path) -> None:
    """Exercise catalog argv shape refusals and direct-launch positive forms."""
    bad_cases = (
        ("empty_argv", []),
        ("empty_token", [".venv/bin/python", ""]),
        ("bare_shell", ["bash", "quality_gates/ruff_repo.sh"]),
        ("absolute_shell", ["/bin/bash", "quality_gates/ruff_repo.sh"]),
    )
    # Invalid rows are rejected during parsing, before any subprocess can run.
    for name, argv in bad_cases:
        key = f"fixture_{name}"
        path = _catalog_path(root, key, argv)
        try:
            coverage.load_catalog(path)
        except coverage.CoverageError as error:
            assert key in str(error), f"CATALOG_ERROR_NAMES_KEY {key}: {error}"
        else:
            raise AssertionError(f"CAPTURE_ARGV_REFUSED {key}")

    path = root / "positive-direct-catalog.json"
    path.write_bytes(
        coverage.canonical(
            {
                "aggregate_classes": "quality_gates.code_quality_scope:CLASS_NAMES",
                "gates": [
                    {"key": "direct_script", "audience": "peer", "applicability": "candidate", "argv": ["quality_gates/ruff_repo.sh", "{paths}"]},
                    {"key": "direct_interpreter", "audience": "peer", "applicability": "candidate", "argv": [".venv/bin/python", "quality_gates/check.py"]},
                ],
            }
        )
    )
    positive = coverage.load_catalog(path)
    assert all(_capture_accepts(gate.argv) for gate in positive.gates), "CAPTURE_DIRECT_POSITIVE"


def _catalog_path(root: Path, key: str, argv: object) -> Path:
    path = root / f"{key}.json"
    path.write_bytes(
        coverage.canonical(
            {
                "aggregate_classes": "quality_gates.code_quality_scope:CLASS_NAMES",
                "gates": [{"key": key, "audience": "peer", "applicability": "candidate", "argv": argv}],
            }
        )
    )
    return path


def _coverage_cli(root: Path, identity: Path, snapshot: Path, manifest: Path, rename_file: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "quality_gates.review_static_coverage",
        "--repo-root",
        str(root / "repo"),
        "--identity",
        str(identity),
        "--snapshot-root",
        str(snapshot),
        "--manifest",
        str(manifest),
        "--delta-file",
        str(root / "delta.paths"),
        "--rename-file",
        str(rename_file),
    ]


def _cli_env() -> dict[str, str]:
    return dict(os.environ, PYTHONPATH="src")


def _cli(root: Path, identity: Path, snapshot: Path, manifest: Path) -> None:
    rename_file = root / "cli-evidence" / "rename-map.txt"
    rename_file.parent.mkdir()
    assert not rename_file.exists(), "IDENTITY_RENAME_ABSENT"
    argv = _coverage_cli(root, identity, snapshot, manifest, rename_file)
    result = subprocess.run(argv, cwd=_ROOT, env=_cli_env(), capture_output=True, check=True)
    # The first plan provisions evidence once; repeated planning must preserve it.
    before = rename_file.read_bytes()
    assert before == b"", "IDENTITY_RENAME_PROVISIONED"
    planned = root / "plan.json"
    planned.write_bytes(result.stdout)
    plan = cast(dict[str, object], json.loads(result.stdout))
    identity_row = next(row for row in cast(list[dict[str, object]], plan["rows"]) if row["gate"] == "identity")
    planned_argv = cast(list[str], identity_row["planned_argv"])
    assert planned_argv[planned_argv.index("--rename-file") + 1] == str(rename_file.resolve()), "IDENTITY_RENAME_ARG"
    assert all("{" not in token and "}" not in token for token in planned_argv), "NO_UNRESOLVED_PLACEHOLDERS"
    repeated = subprocess.run(argv, cwd=_ROOT, env=_cli_env(), capture_output=True, check=True)
    assert repeated.stdout == result.stdout and rename_file.read_bytes() == before, "IDENTITY_RENAME_REPEAT"
    reread = subprocess.run([*argv, "--read-report", str(planned)], cwd=_ROOT, env=_cli_env(), capture_output=True, check=True)
    assert result.stdout == reread.stdout, "CLI_READBACK"
    assert json.loads(result.stdout)["status"] == "PARTIAL", "PLAN_NOT_EXECUTION"


def _run_cli(
    root: Path,
    identity: Path,
    snapshot: Path,
    manifest: Path,
    rename_file: Path,
    *extra: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*_coverage_cli(root, identity, snapshot, manifest, rename_file), *extra],
        cwd=_ROOT,
        env=_cli_env(),
        capture_output=True,
        check=False,
    )


def _rename_inputs(
    root: Path,
    subject: coverage.Subject,
    identity: Path,
    snapshot: Path,
    manifest: Path,
    catalog: coverage.Catalog,
) -> None:
    """Exercise the create/read/revalidate and invalid rename-file inputs."""
    repo = Path(subject.repo_root)
    delta = Path(subject.delta_file)
    rename_file = Path(subject.rename_file)
    original = rename_file.read_bytes()
    assert original == b"", "IDENTITY_RENAME_PROVISIONED"
    reused = coverage.load_subject(repo, identity, snapshot, manifest, delta, rename_file)
    assert reused.rename_file == subject.rename_file, "IDENTITY_RENAME_RESOLVED"
    assert rename_file.read_bytes() == original, "IDENTITY_RENAME_REUSED"
    # Failures below leave absent parents, directories, symlinks, and bytes untouched.
    _rename_parent(root, identity, snapshot, manifest)
    _rename_nonregular(root, identity, snapshot, manifest)
    _rename_symlink(root, identity, snapshot, manifest)
    _rename_nonempty(root, subject, identity, snapshot, manifest, catalog, original)


def _rename_parent(root: Path, identity: Path, snapshot: Path, manifest: Path) -> None:
    missing_parent = root / "missing-parent" / "rename-map.txt"
    absent = _run_cli(root, identity, snapshot, manifest, missing_parent)
    assert absent.returncode != 0, "RENAME_PARENT_REQUIRED"
    assert not missing_parent.parent.exists(), "RENAME_PARENT_REQUIRED"


def _rename_nonregular(root: Path, identity: Path, snapshot: Path, manifest: Path) -> None:
    directory = root / "rename-directory"
    directory.mkdir()
    nonregular = _run_cli(root, identity, snapshot, manifest, directory)
    assert nonregular.returncode != 0, "RENAME_NONREGULAR_REFUSED"
    assert directory.is_dir(), "RENAME_NONREGULAR_UNCHANGED"


def _rename_symlink(root: Path, identity: Path, snapshot: Path, manifest: Path) -> None:
    target = root / "rename-target.txt"
    target.write_bytes(b"")
    link = root / "rename-link.txt"
    link.symlink_to(target)
    linked = _run_cli(root, identity, snapshot, manifest, link)
    assert linked.returncode != 0, "RENAME_SYMLINK_REFUSED"
    assert link.is_symlink(), "RENAME_SYMLINK_UNCHANGED"
    assert target.read_bytes() == b"", "RENAME_SYMLINK_TARGET_UNCHANGED"


def _rename_nonempty(
    root: Path,
    subject: coverage.Subject,
    identity: Path,
    snapshot: Path,
    manifest: Path,
    catalog: coverage.Catalog,
    original: bytes,
) -> None:
    rename_file = Path(subject.rename_file)
    protected = b"preserve these bytes\n"
    rename_file.write_bytes(protected)
    invalid = _run_cli(root, identity, snapshot, manifest, rename_file)
    assert invalid.returncode != 0, "NONEMPTY_RENAME_REFUSED"
    assert rename_file.read_bytes() == protected, "NONEMPTY_RENAME_UNCHANGED"
    claim = _claim(root, subject, catalog, "identity", subject.changed)
    _refused(lambda: coverage.report(subject, catalog, [claim]), "NONEMPTY_RENAME_REFUSED")
    assert rename_file.read_bytes() == protected, "NONEMPTY_RENAME_UNCHANGED"
    rename_file.write_bytes(original)


def _all_catalog_claims(root: Path, subject: coverage.Subject, catalog: coverage.Catalog) -> None:
    """Accept exact planned argv and reject an altered claim for every key."""
    for gate in catalog.gates:
        selected = subject.changed if gate.applicability == "candidate" else subject.live
        claim = _claim(root, subject, catalog, gate.key, selected)
        accepted = coverage.report(subject, catalog, [claim])
        row = _rows(accepted)[gate.key]
        # Reviewer claims are measured here; controller rows remain explicitly deferred.
        if gate.audience == "controller":
            assert row["status"] == "CONTROLLER-DEFERRED", f"CONTROLLER_DEFERRED {gate.key}"
        else:
            assert row["status"] == "PASS", f"EXACT_CATALOG_ARGV {gate.key}"

        changed_argv = list(cast(list[str], claim["argv"]))
        changed_argv[-1] = f"{changed_argv[-1]}.altered"
        receipt_path = Path(str(claim["evidence_ref"]))
        receipt = cast(dict[str, object], json.loads(receipt_path.read_bytes()))
        receipt["argv"] = changed_argv
        receipt_path.write_bytes(coverage.canonical(receipt))
        altered = dict(claim, argv=changed_argv, evidence_sha256=coverage.digest(receipt_path.read_bytes()))
        _refused(
            lambda altered=altered: coverage.report(subject, catalog, [altered]),
            f"EXACT_CATALOG_ARGV {gate.key}",
        )
    print("PASS EXACT_CATALOG_ARGV all catalog keys; Controller rows deferred")


def _identity_tree(root: Path, subject: coverage.Subject, catalog: coverage.Catalog) -> None:
    """Run candidate_tree with this smoke's interpreter and the planned argv."""
    stale = "COVERAGE_STALE_IDENTITY_GATE_RAN"
    # The working tree is deliberately stale; the planned command must reach the commit tree.
    _write(Path(subject.repo_root), _IDENTITY_GATE, f'print("{stale}")\n')
    claim = _claim(root, subject, catalog, "identity", subject.changed)
    accepted = coverage.report(subject, catalog, [claim])
    planned = cast(list[str], _rows(accepted)["identity"]["planned_argv"])
    invoking_argv = [sys.executable, *planned[1:]]
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("GIT_"):
            environment.pop(key)
    try:
        result = subprocess.run(
            invoking_argv,
            cwd=_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        _write(Path(subject.repo_root), _IDENTITY_GATE, f'print("{_CANDIDATE_SENTINEL}")\n')
    combined = result.stdout + result.stderr
    assert stale not in combined, f"IDENTITY_STALE_GATE {combined}"
    assert _CANDIDATE_SENTINEL in combined, f"IDENTITY_CANDIDATE_TREE_REACHED {combined}"
    assert result.returncode == 0, (result.returncode, combined)
    print("PASS IDENTITY_CANDIDATE_TREE_REACHED with complete no-renames delta")


def _native_edges(
    root: Path,
    subject: coverage.Subject,
    identity: Path,
    snapshot: Path,
    manifest: Path,
) -> coverage.Subject:
    repo = root / "repo"
    old, new = "ananta/src/fixture/case_00.py", "solet_cli/tests/renamed.py"
    base = _git(repo, "rev-parse", "HEAD").decode().strip()
    data = (repo / old).read_bytes()
    (repo / old).rename(repo / new)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "rename")
    commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    entry = {"mode": 0o100644, "sha256": coverage.digest(data)}
    raw = cast(dict[str, object], json.loads(identity.read_bytes()))
    raw.update(base_commit_sha=base, candidate_commit_sha=commit, candidate_tree_sha=_git(repo, "rev-parse", "HEAD^{tree}").decode().strip(), entries=[{"path": old, "base": entry, "final": "absent"}, {"path": new, "base": "absent", "final": entry}])
    raw["patch_sha256"] = coverage.digest(_git(repo, "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", base, commit))
    identity.write_bytes(coverage.canonical(raw))
    (snapshot / old).unlink()
    _write(snapshot, new, data.decode())
    paths = sorted(path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file())
    manifest.write_text("".join(path + "\n" for path in paths))
    delta_file = _scope_file(root, (old, new))
    renamed_subject = coverage.load_subject(
        repo,
        identity,
        snapshot,
        manifest,
        delta_file,
        Path(subject.rename_file),
    )
    assert renamed_subject.changed == (old, new) and renamed_subject.deleted == (old,) and renamed_subject.live == (new,), "NATIVE_RENAME_DELTA"
    broken = dict(raw, entries=[])
    identity.write_bytes(coverage.canonical(broken))
    _refused(
        lambda: coverage.load_subject(
            repo,
            identity,
            snapshot,
            manifest,
            delta_file,
            Path(subject.rename_file),
        ),
        "NATIVE_DELTA_SUBSET",
    )
    identity.write_bytes(coverage.canonical(raw))
    return renamed_subject


def _scope_changed(subject: coverage.Subject, catalog: coverage.Catalog, claims: list[dict[str, object]]) -> None:
    path = Path(subject.delta_file)
    before = path.read_bytes()
    try:
        path.write_text(subject.changed[0] + "\n")
        _refused(lambda: coverage.report(subject, catalog, claims), "IDENTITY_SCOPE_BOUND")
    finally:
        path.write_bytes(before)


def _identity_binding(root: Path, subject: coverage.Subject, catalog: coverage.Catalog) -> None:
    """P1: the reviewer's forty-zero base-ref input must be refused."""
    claims = [_claim(root, subject, catalog, gate.key, subject.changed if gate.applicability == "candidate" else subject.live) for gate in catalog.gates if gate.audience == "peer"]
    original = next(claim for claim in claims if claim["gate"] == "identity")
    variants = (
        ("zero", "--base-ref", "0" * 40),
        ("unresolvable", "--base-ref", "f" * 40),
        ("repo", "--repo-root", str(root / "other-repo")),
        ("scope", "--scope-file", str(root / "other.paths")),
        ("rename", "--rename-file", str(root / "other-renames.txt")),
    )
    for name, flag, value in variants:
        argv = list(cast(list[str], original["argv"]))
        argv[argv.index(flag) + 1] = value
        receipt = cast(dict[str, object], json.loads(Path(str(original["evidence_ref"])).read_bytes()))
        receipt["argv"] = argv
        evidence = root / f"identity-{name}.json"
        evidence.write_bytes(coverage.canonical(receipt))
        changed = dict(original, argv=argv, evidence_ref=str(evidence), evidence_sha256=coverage.digest(evidence.read_bytes()))
        inputs = [changed if claim["gate"] == "identity" else claim for claim in claims]
        _refused(lambda inputs=inputs: coverage.report(subject, catalog, inputs), "IDENTITY_ARG_BOUND")
    _scope_changed(subject, catalog, claims)
    subset = _claim(root, subject, catalog, "identity", subject.changed[:1])
    _refused(lambda: coverage.report(subject, catalog, [subset]), "IDENTITY_SELECTED_BOUND")
    print("PASS IDENTITY_ARG_BOUND IDENTITY_SCOPE_BOUND")


def _empty_claim(root: Path, subject: coverage.Subject, catalog: coverage.Catalog) -> None:
    """P2: preserve and validate supplied claims even with no Python delta."""
    nonpy = replace(subject, changed=("notes.md",), live=("notes.md",), deleted=())
    unsafe: dict[str, object] = {**_binding(nonpy, catalog), "gate": "radon_cc", "selected": ["../unsafe.py"]}
    _refused(lambda: coverage.report(nonpy, catalog, [unsafe]), "EMPTY_SCOPE_CLAIM")
    incomplete = dict(unsafe, selected=[])
    _refused(lambda: coverage.report(nonpy, catalog, [incomplete]), "EMPTY_SCOPE_METADATA")
    good = _claim(root, nonpy, catalog, "radon_cc", ())
    absent = dict(good)
    absent.pop("evidence_ref")
    _refused(lambda: coverage.report(nonpy, catalog, [absent]), "EMPTY_SCOPE_EVIDENCE")
    result = coverage.report(nonpy, catalog, [good])
    assert _rows(result)["radon_cc"].get("claim") == good, "EMPTY_SCOPE_PRESERVED"
    coverage.validate_report(result, nonpy, catalog)
    extra = _claim(root, nonpy, catalog, "radon_cc", ("quality_gates/unexpected.py",))
    row = _rows(coverage.report(nonpy, catalog, [extra]))["radon_cc"]
    assert row["status"] == "PARTIAL" and row["unexpected"] == ["quality_gates/unexpected.py"] and row["claim"] == extra, "EMPTY_SCOPE_UNEXPECTED"
    print("PASS EMPTY_SCOPE_CLAIM EMPTY_SCOPE_PRESERVED")


def main() -> int:
    catalog = _load_catalog()
    _catalog(catalog)
    with tempfile.TemporaryDirectory(prefix="review_static_coverage_") as tmp:
        root = Path(tmp).resolve()
        _catalog_inputs(root)
        subject, identity, snapshot, manifest = _fixture(root)
        _cli(root, identity, snapshot, manifest)
        _partial(root, subject, catalog)
        _complete(root, subject, catalog)
        _all_catalog_claims(root, subject, catalog)
        _bad_claims(root, subject, catalog)
        _rename_inputs(root, subject, identity, snapshot, manifest, catalog)
        _edges(subject, catalog)
        _identity_binding(root, subject, catalog)
        _empty_claim(root, subject, catalog)
        _identity_tree(root, subject, catalog)
        renamed_subject = _native_edges(root, subject, identity, snapshot, manifest)
        rename_claim = _claim(root, renamed_subject, catalog, "identity", renamed_subject.changed)
        coverage.report(renamed_subject, catalog, [rename_claim])
        print("PASS NATIVE_RENAME_DELTA with empty rename mapping")
    print("ALL STATIC COVERAGE CHECKS PASSED (fixture receipts; no Controller execution)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
