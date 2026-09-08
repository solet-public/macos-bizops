"""Focused no-network smoke for the release-payload staging script.

Builds a throwaway local git fixture standing in for a checked-out seed
release, runs ``stage_release.py`` against it twice, and checks: the payload
archive excludes the rendered lock file, the checksum in the metadata
matches the archive's real bytes, the render step's own invariants still
hold, and staging the same commit twice is byte-for-byte reproducible. No
network access, no GitHub, no git mutation of the real checkout.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "stage_release.py"
_RENDERER = _ROOT / "scripts" / "render_release_payload.py"
_PLUGIN_SRC = _REPOSITORY_ROOT / "plugins" / "github_midwife_plugin" / "src"
_SETUP_CONTRACTS_SRC = _REPOSITORY_ROOT / "solet_setup_contracts" / "src"
sys.path.insert(0, str(_ROOT.parent / "src"))
sys.path.insert(0, str(_PLUGIN_SRC))
sys.path.insert(0, str(_SETUP_CONTRACTS_SRC))

from github_midwife_plugin.permissions_manifest import load_flow, render_manifest  # noqa: E402
from solet_manager.contracts import (  # noqa: E402
    PERMISSIONS_MANIFEST_FILENAME,
    ContractBundle,
    contract_digest,
    contract_digested_filenames,
    contract_filenames,
)
from solet_manager.errors import ContractError  # noqa: E402
from solet_manager.permission_preflight import render_permission_preflight  # noqa: E402
from solet_manager.plan_builder import SetupPlan  # noqa: E402
from solet_setup_contracts.selected_source_record import (  # noqa: E402
    SelectedSourceTransaction,
    validate_target_contract_identity,
)

_CONTRACT_ARCHIVE_ROOT = "plugins/github_midwife_plugin/knowledge_base"
_CONTRACT_SOURCE = _REPOSITORY_ROOT / _CONTRACT_ARCHIVE_ROOT
_CANONICAL_SEED_REPOSITORY = "https://github.com/solet-public/macos-bizops.git"
_CANONICAL_SEED_PROFILE = "macos-bizops"
_RELEASE_TAG = "release-2026-08-23"
_MANAGER_REPOSITORY = "https://github.com/solet-public/homebrew-tap.git"
_MANAGER_RELEASE_TAG = "manager-v0.1.0-r0"
_MANAGER_SOURCE_REPOSITORY = "https://github.com/solet-public/solet.git"
_OTHER_SEED_REPOSITORY = "https://github.com/solet-public/macos-samantha.git"
_OTHER_SEED_PROFILE = "macos-samantha"
_FIXTURE_LICENSE = b"fixture Apache-2.0 license\n"
_FIXTURE_NOTICE = b"fixture notice\n"
_FORMULA_BUILD_PATH_INSTALL = re.compile(r'venv\.pip_install buildpath/"(?P<path>[^"]+)"')
_FORMULA_CONTRACT_INSTALL = re.compile(
    rf'"{re.escape(_CONTRACT_ARCHIVE_ROOT)}/(?P<filename>[^"]+)"'
)
_checks = 0


@dataclass(frozen=True)
class _ContractFilenameConsumer:
    path: Path
    extractor: Callable[[Path], set[str]]
    authority: Callable[[], tuple[str, ...]]
    authority_name: str


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "stage-release-smoke",
            "GIT_AUTHOR_EMAIL": "stage-release-smoke@example.invalid",
            "GIT_COMMITTER_NAME": "stage-release-smoke",
            "GIT_COMMITTER_EMAIL": "stage-release-smoke@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    )


def _build_seed_fixture(root: Path) -> tuple[Path, Path, str]:
    """A minimal committed tree carrying only what stage_release.py needs."""
    checkout = root / "seed-checkout"
    checkout.mkdir()
    (checkout / "LICENSE").write_bytes(_FIXTURE_LICENSE)
    (checkout / "NOTICE").write_bytes(_FIXTURE_NOTICE)
    (checkout / "solet_cli" / "homebrew").mkdir(parents=True)
    (checkout / "solet_cli" / "pyproject.toml").write_text(
        '[project]\nname = "solet-cli"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (checkout / "solet_cli" / "src.marker").write_text("manager source\n", encoding="utf-8")
    (checkout / "solet_cli" / "homebrew" / "seed.lock.json.template").write_text(
        "{}\n", encoding="utf-8"
    )
    (checkout / "solet_setup_contracts").mkdir()
    (checkout / "solet_setup_contracts" / "pyproject.toml").write_text(
        '[project]\nname = "solet-setup-contracts"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    contracts = checkout / _CONTRACT_ARCHIVE_ROOT
    contracts.mkdir(parents=True)
    for name in contract_filenames():
        (contracts / name).write_bytes((_CONTRACT_SOURCE / name).read_bytes())

    _run_git(checkout, "init", "-q")
    _run_git(checkout, "add", "-A")
    _run_git(checkout, "commit", "-q", "-m", "seed fixture")
    _run_git(checkout, "tag", _RELEASE_TAG)
    manager_ref = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()
    manager_checkout = root / "manager-checkout"
    _run_git(
        checkout,
        "worktree",
        "add",
        "--detach",
        "-q",
        str(manager_checkout),
        manager_ref,
    )
    (checkout / "shared-head.marker").write_text("shared checkout moved\n", encoding="utf-8")
    _run_git(checkout, "add", "shared-head.marker")
    _run_git(checkout, "commit", "-q", "-m", "shared checkout moves after pin")
    _run_git(checkout, "branch", "release-branch")
    return checkout, manager_checkout, manager_ref


def _stage(
    seed_checkout: Path, manager_checkout: Path, manager_ref: str, output_root: Path
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--seed-checkout",
            str(seed_checkout),
            "--release-tag",
            _RELEASE_TAG,
            "--seed-repository",
            _CANONICAL_SEED_REPOSITORY,
            "--seed-profile",
            _CANONICAL_SEED_PROFILE,
            "--manager-repository",
            _MANAGER_REPOSITORY,
            "--manager-release-tag",
            _MANAGER_RELEASE_TAG,
            "--manager-checkout",
            str(manager_checkout),
            "--manager-ref",
            manager_ref,
            "--manager-source-repository",
            _MANAGER_SOURCE_REPOSITORY,
            "--formula-revision",
            "0",
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(result.returncode == 0, f"stage_release.py failed: {result.stderr}")
    return result


def _stage_lock_only(checkout: Path, output_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--seed-checkout",
            str(checkout),
            "--release-tag",
            _RELEASE_TAG,
            "--seed-repository",
            _OTHER_SEED_REPOSITORY,
            "--seed-profile",
            _OTHER_SEED_PROFILE,
            "--lock-only",
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _payload_archive_path(output_root: Path) -> Path:
    payload_dir = output_root / "payload"
    archives = sorted(payload_dir.glob("solet-*.tar.gz"))
    _check(len(archives) == 1, f"exactly one payload archive staged, found {archives}")
    return archives[0]


def _check_metadata(
    metadata: dict[str, object],
    expected_commit: str,
    expected_tree: str,
    expected_manager_ref: str,
    expected_manager_commit: str,
    expected_manager_tree: str,
) -> None:
    _check(metadata["seed_repository"] == _CANONICAL_SEED_REPOSITORY, "canonical repository")
    _check(metadata["seed_release_tag"] == _RELEASE_TAG, "tag carried through")
    _check(metadata["seed_commit"] == expected_commit, "commit resolved from the tag")
    _check(metadata["seed_tree_hash"] == expected_tree, "tree hash resolved from the commit")
    _check(
        metadata["manager_source_repository"] == _MANAGER_SOURCE_REPOSITORY,
        "manager source repository is carried through",
    )
    _check(
        "manager_source_ref" in metadata,
        "release metadata records the supplied manager source ref",
    )
    _check(
        metadata["manager_source_ref"] == expected_manager_ref,
        "manager source ref records the supplied immutable pin",
    )
    _check(
        metadata["manager_source_commit"] == expected_manager_commit,
        "manager source commit resolved from the manager checkout",
    )
    _check(
        metadata["manager_source_tree_hash"] == expected_manager_tree,
        "manager source tree hash resolved from the manager checkout",
    )
    _check(
        metadata["manager_url"]
        == "https://github.com/solet-public/homebrew-tap/releases/download/"
        f"{_MANAGER_RELEASE_TAG}/solet-0.1.0.tar.gz",
        "manager_url carries the independent public manager repository and tag",
    )


def _formula_buildpath_installs(formula: str) -> tuple[str, ...]:
    return tuple(match.group("path") for match in _FORMULA_BUILD_PATH_INSTALL.finditer(formula))


def _missing_archive_install_paths(
    archive_members: set[str], install_paths: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(
        path
        for path in install_paths
        if not any(member == path or member.startswith(f"{path}/") for member in archive_members)
    )


def _check_archive(
    archive_path: Path, metadata: dict[str, object], formula: str
) -> str:
    real_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    _check(
        metadata["release_archive_sha256"] == real_sha256,
        "declared checksum matches the payload archive's real bytes",
    )
    with tarfile.open(archive_path, mode="r:gz") as archive:
        names = set(archive.getnames())
        license_member = archive.extractfile("LICENSE") if "LICENSE" in names else None
        notice_member = archive.extractfile("NOTICE") if "NOTICE" in names else None
        distributed_license = license_member.read() if license_member is not None else None
        distributed_notice = notice_member.read() if notice_member is not None else None
    _check(
        "solet_cli/src.marker" in names
        and "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json" in names,
        "payload carries the manager source and the setup contracts",
    )
    _check_contract_shipment_parity(names)
    install_paths = _formula_buildpath_installs(formula)
    _check(bool(install_paths), "rendered Formula installs at least one payload-local package")
    missing_install_paths = _missing_archive_install_paths(names, install_paths)
    _check(
        not missing_install_paths,
        "payload carries every Formula buildpath install: "
        + ", ".join(missing_install_paths),
    )
    _check(
        "solet_cli/homebrew/seed.lock.json" not in names,
        "payload excludes the rendered lock — it ships in the tap, not the download, "
        "or the checksum would describe an archive that contains its own checksum",
    )
    _check(
        distributed_license is not None
        and distributed_license == _FIXTURE_LICENSE,
        "payload carries the fixture LICENSE byte-for-byte",
    )
    _check(
        distributed_notice is not None
        and distributed_notice == _FIXTURE_NOTICE,
        "payload carries the fixture NOTICE byte-for-byte",
    )
    return real_sha256


def _check_contract_shipment_parity(archive_members: set[str]) -> None:
    expected = set(contract_filenames())
    archive_prefix = f"{_CONTRACT_ARCHIVE_ROOT}/"
    archived = {
        member.removeprefix(archive_prefix)
        for member in archive_members
        if member.startswith(archive_prefix)
    }
    _check(
        archived == expected,
        "payload contract files exactly match ContractBundle's required-file authority",
    )
    _check_contract_consumer_parity()
    _check(
        _bundle_directory_read_filenames() <= expected,
        "every CLI filename read below bundle.directory belongs to the shipped contract set",
    )


def _check_contract_consumer_parity() -> None:
    for consumer in _CONTRACT_FILENAME_CONSUMERS:
        expected = set(consumer.authority())
        _check(
            consumer.extractor(consumer.path) == expected,
            f"{consumer.path.relative_to(_REPOSITORY_ROOT)} exactly matches "
            f"the {consumer.authority_name} contract authority",
        )


def _extract_stage_contract_filenames(path: Path) -> set[str]:
    return _contract_filenames_from_paths(
        _extract_module_tuple_strings(path, "_CONTRACT_PATHS")
    )


def _extract_stage_digested_contract_filenames(path: Path) -> set[str]:
    return _contract_filenames_from_paths(
        _extract_module_tuple_strings(path, "_DIGESTED_CONTRACT_PATHS")
    )


def _extract_selected_source_contract_filenames(path: Path) -> set[str]:
    return set(_extract_module_tuple_strings(path, "_CONTRACT_FILENAMES"))


def _extract_module_tuple_strings(path: Path, constant_name: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tuples = _module_tuple_assignments(tree)
    value = tuples.get(constant_name)
    if value is None:
        raise AssertionError(f"red: no {constant_name} tuple in {path}")
    return _tuple_string_literals(value, tuples, resolving={constant_name})


def _module_tuple_assignments(tree: ast.Module) -> dict[str, ast.Tuple]:
    tuples: dict[str, ast.Tuple] = {}
    for assignment in tree.body:
        if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
            continue
        target = assignment.targets[0]
        if isinstance(target, ast.Name) and isinstance(assignment.value, ast.Tuple):
            tuples[target.id] = assignment.value
    return tuples


def _tuple_string_literals(
    value: ast.Tuple,
    tuples: dict[str, ast.Tuple],
    *,
    resolving: set[str],
) -> list[str]:
    values: list[str] = []
    for item in value.elts:
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            values.append(item.value)
            continue
        if isinstance(item, ast.Starred) and isinstance(item.value, ast.Name):
            name = item.value.id
            nested = tuples.get(name)
            if nested is None or name in resolving:
                raise AssertionError(f"red: contract tuple star cannot resolve local {name}")
            values.extend(
                _tuple_string_literals(nested, tuples, resolving=resolving | {name})
            )
            continue
        raise AssertionError("red: contract tuple contains a non-literal or unresolved path")
    return values


def _extract_formula_contract_filenames(path: Path) -> set[str]:
    formula = path.read_text(encoding="utf-8")
    return {match.group("filename") for match in _FORMULA_CONTRACT_INSTALL.finditer(formula)}


def _contract_filenames_from_paths(paths: list[str]) -> set[str]:
    prefix = f"{_CONTRACT_ARCHIVE_ROOT}/"
    if not all(path.startswith(prefix) for path in paths):
        raise AssertionError("red: contract consumer path is outside the shipped contracts directory")
    return {path.removeprefix(prefix) for path in paths}


_CONTRACT_FILENAME_CONSUMERS = (
    _ContractFilenameConsumer(
        path=_ROOT / "scripts" / "stage_release.py",
        extractor=_extract_stage_digested_contract_filenames,
        authority=contract_digested_filenames,
        authority_name="digested",
    ),
    _ContractFilenameConsumer(
        path=_ROOT / "scripts" / "stage_release.py",
        extractor=_extract_stage_contract_filenames,
        authority=contract_filenames,
        authority_name="shipped",
    ),
    _ContractFilenameConsumer(
        path=_ROOT / "Formula" / "solet.rb.template",
        extractor=_extract_formula_contract_filenames,
        authority=contract_filenames,
        authority_name="shipped",
    ),
    _ContractFilenameConsumer(
        path=_REPOSITORY_ROOT
        / "solet_setup_contracts"
        / "src"
        / "solet_setup_contracts"
        / "selected_source_record.py",
        extractor=_extract_selected_source_contract_filenames,
        authority=contract_digested_filenames,
        authority_name="digested",
    ),
)


def _bundle_directory_read_filenames() -> set[str]:
    filenames: set[str] = set()
    source_root = _ROOT.parent / "src" / "solet_manager"
    for source in source_root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        constants = _module_string_constants(tree)
        for node in ast.walk(tree):
            filename = _bundle_directory_read_filename(node, constants)
            if filename is not None:
                filenames.add(filename)
    return filenames


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for assignment in tree.body:
        if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
            continue
        target = assignment.targets[0]
        value = assignment.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[target.id] = value.value
    return constants


def _bundle_directory_read_filename(node: ast.AST, constants: dict[str, str]) -> str | None:
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
        return None
    if not isinstance(node.left, ast.Attribute) or node.left.attr != "directory":
        return None
    if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
        return node.right.value
    if isinstance(node.right, ast.Name):
        return constants.get(node.right.id)
    return None


def _check_extracted_contract_bundle(
    archive_path: Path,
    expected_commit: str,
    staged_contract_digest: str,
    root: Path,
) -> None:
    extracted = root / "extracted-payload"
    with tarfile.open(archive_path, mode="r:gz") as archive:
        archive.extractall(extracted, filter="data")
    contracts = extracted / _CONTRACT_ARCHIVE_ROOT
    bundle = ContractBundle.load(source_revision=expected_commit, directory=contracts)
    preflight = render_permission_preflight(
        bundle,
        SetupPlan(
            answers={
                "decisions": {
                    "setup_profile": "free",
                    "autostart": "disabled",
                    "coding_agents": ["codex"],
                    "session_sources": ["codex_local"],
                },
                "consents": {"system_change_consent": True},
            },
            operations=(),
            unresolved_decisions=(),
            unresolved_consents=(),
        ),
        {},
    )
    _check(
        preflight["manifest"] == PERMISSIONS_MANIFEST_FILENAME,
        "preflight reads the manifest loaded from the extracted ContractBundle",
    )
    _check(
        bundle.contract_digest == staged_contract_digest,
        "manifest is shipped and load-required but excluded from flow reconciliation identity",
    )
    _check_manifest_matches_flow(contracts)
    _check_manifest_drift_mutation_is_red(contracts)
    (contracts / PERMISSIONS_MANIFEST_FILENAME).unlink()
    try:
        ContractBundle.load(source_revision=expected_commit, directory=contracts)
    except ContractError as exc:
        _check(
            PERMISSIONS_MANIFEST_FILENAME in str(exc),
            "missing manifest is refused at ContractBundle load",
        )
    else:
        raise AssertionError("red: r21-style manifest omission reached preflight instead of load refusal")


def _check_manifest_matches_flow(contracts: Path) -> None:
    expected = render_manifest(load_flow(contracts / "macos_setup_flow.json")).json_bytes
    actual = (contracts / PERMISSIONS_MANIFEST_FILENAME).read_bytes()
    _check(actual == expected, "extracted manifest exactly matches its setup-flow rendering")


def _check_manifest_drift_mutation_is_red(contracts: Path) -> None:
    manifest = json.loads((contracts / PERMISSIONS_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        raise AssertionError("red: fixture manifest has no mutable entry")
    entries[0]["why"] = "MUTATED"
    mutated = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    try:
        _assert_manifest_matches_flow(mutated, contracts)
    except AssertionError:
        _check(True, "mutated extracted manifest is RED against its setup-flow rendering")
        return
    raise AssertionError("red: mutated extracted manifest passed the flow-rendering drift check")


def _assert_manifest_matches_flow(actual: bytes, contracts: Path) -> None:
    expected = render_manifest(load_flow(contracts / "macos_setup_flow.json")).json_bytes
    if actual != expected:
        raise AssertionError("manifest does not match its setup-flow rendering")


def _staged_contract_digest(stage_result: subprocess.CompletedProcess[str]) -> str:
    match = re.search(r"setup contracts:\s+(sha256:[0-9a-f]{64})", stage_result.stdout)
    _check(match is not None, "stager reports its validated flow contract digest")
    if match is None:
        raise AssertionError("red: stager did not report a flow contract digest")
    return match.group(1)


def _check_contract_digest_implementation_parity(
    checkout: Path,
    staged_contract_digest: str,
) -> None:
    manager_digest = contract_digest(checkout / _CONTRACT_ARCHIVE_ROOT)
    _check(
        manager_digest == staged_contract_digest,
        "manager and release stager compute the same setup-contract identity",
    )
    transaction = SelectedSourceTransaction(
        name="fixture",
        target=str(checkout),
        answers={},
        answers_fingerprint="fixture",
        flow_source_revision="fixture",
        flow_contract_digest=manager_digest,
    )
    validate_target_contract_identity(transaction, target=checkout)
    _check(
        True,
        "selected-source validation computes the same setup-contract identity as the manager",
    )


def _check_archive_guard_rejects_pre_fix_omission() -> None:
    missing = _missing_archive_install_paths(
        {
            "solet_cli/src.marker",
            "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json",
        },
        ("solet_setup_contracts", "solet_cli"),
    )
    _check(
        missing == ("solet_setup_contracts",),
        "archive guard rejects the pre-fix omission that the former spot-check missed",
    )


def _check_rendered_outputs(output_root: Path, real_sha256: str) -> None:
    formula = (output_root / "Formula" / "solet.rb").read_text(encoding="utf-8")
    lock = json.loads(
        (output_root / "solet_cli" / "homebrew" / "seed.lock.json").read_text(
            encoding="utf-8"
        )
    )
    _check(str(real_sha256) in formula, "rendered formula carries the real checksum")
    _check(lock["archive_sha256"] == real_sha256, "rendered lock carries the real checksum")
    _check(
        "Pathname(__dir__)" in formula
        and "buildpath" not in formula.split("seed.lock")[0][-80:],
        "formula installs the lock from the tap directory, not the downloaded payload",
    )


def _check_staged_metadata_is_rerenderable(output_root: Path) -> None:
    rerender_root = output_root.parent / "rerendered"
    result = subprocess.run(
        [
            sys.executable,
            str(_RENDERER),
            "--metadata",
            str(output_root / "release_metadata.json"),
            "--output-root",
            str(rerender_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(result.returncode == 0, f"staged metadata re-renders: {result.stderr}")
    for relative_path in ("Formula/solet.rb", "solet_cli/homebrew/seed.lock.json"):
        _check(
            (output_root / relative_path).read_bytes() == (rerender_root / relative_path).read_bytes(),
            f"re-rendered {relative_path} is byte-identical",
        )


def _check_lock_only(
    checkout: Path,
    output_root: Path,
    expected_commit: str,
    expected_tree: str,
) -> None:
    result = _stage_lock_only(checkout, output_root)
    _check(result.returncode == 0, f"stage_release.py --lock-only failed: {result.stderr}")
    _check(not (output_root / "payload").exists(), "--lock-only builds no payload archive")
    _check(not (output_root / "Formula").exists(), "--lock-only renders no Formula")
    lock = json.loads((output_root / "seed.lock.json").read_text(encoding="utf-8"))
    _check(
        lock["repository"] == _OTHER_SEED_REPOSITORY
        and lock["profile"] == _OTHER_SEED_PROFILE
        and lock["release_tag"] == _RELEASE_TAG,
        "--lock-only lock carries the given (repository, profile, tag), "
        "not the canonical seed's",
    )
    _check(
        lock["commit"] == expected_commit and lock["tree_hash"] == expected_tree,
        "--lock-only resolves commit/tree_hash from real git, same core as the full path",
    )
    _check(
        "archive_sha256" not in lock,
        "--lock-only omits archive_sha256 entirely — absence, not a placeholder value",
    )


def _check_refused_invocation(checkout: Path) -> None:
    manager_ref = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--seed-checkout",
            str(checkout),
            "--release-tag",
            _RELEASE_TAG,
            "--seed-repository",
            _CANONICAL_SEED_REPOSITORY,
            "--seed-profile",
            _CANONICAL_SEED_PROFILE,
            "--manager-repository",
            _MANAGER_REPOSITORY,
            "--manager-release-tag",
            _MANAGER_RELEASE_TAG,
            "--manager-checkout",
            str(checkout),
            "--manager-ref",
            manager_ref,
            "--manager-source-repository",
            _MANAGER_SOURCE_REPOSITORY,
            "--output-root",
            str(checkout / "refused-output"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(
        result.returncode != 0
        and "--output-root must be outside every source tree" in result.stderr,
        f"stage_release.py refuses an output root inside its source tree: {result.stderr}",
    )


def _check_refused_manager_refs(checkout: Path) -> None:
    for manager_ref in ("HEAD", "main", "master", "latest", "release-branch"):
        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--seed-checkout",
                str(checkout),
                "--release-tag",
                _RELEASE_TAG,
                "--seed-repository",
                _CANONICAL_SEED_REPOSITORY,
                "--seed-profile",
                _CANONICAL_SEED_PROFILE,
                "--manager-repository",
                _MANAGER_REPOSITORY,
                "--manager-release-tag",
                _MANAGER_RELEASE_TAG,
                "--manager-checkout",
                str(checkout),
                "--manager-ref",
                manager_ref,
                "--manager-source-repository",
                _MANAGER_SOURCE_REPOSITORY,
                "--output-root",
                str(checkout.parent / f"refused-{manager_ref}"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        _check(
            result.returncode != 0
            and "--manager-ref has an invalid immutable identity shape" in result.stderr,
            f"stage_release.py refuses moving --manager-ref {manager_ref!r}: {result.stderr}",
        )


def _check_manager_checkout_must_be_pinned(
    checkout: Path, manager_ref: str, shared_head: str, output_root: Path
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--seed-checkout",
            str(checkout),
            "--release-tag",
            _RELEASE_TAG,
            "--seed-repository",
            _CANONICAL_SEED_REPOSITORY,
            "--seed-profile",
            _CANONICAL_SEED_PROFILE,
            "--manager-repository",
            _MANAGER_REPOSITORY,
            "--manager-release-tag",
            _MANAGER_RELEASE_TAG,
            "--manager-checkout",
            str(checkout),
            "--manager-ref",
            manager_ref,
            "--manager-source-repository",
            _MANAGER_SOURCE_REPOSITORY,
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(
        result.returncode != 0
        and "--manager-checkout must be a Git-Controller-provided worktree pinned" in result.stderr
        and manager_ref in result.stderr
        and shared_head in result.stderr,
        f"stage_release.py refuses a manager checkout not pinned at --manager-ref: {result.stderr}",
    )


def _check_manager_checkout_must_be_clean(
    checkout: Path, manager_ref: str, output_root: Path
) -> None:
    dirty_path = checkout / "dirty-manager.marker"
    dirty_path.write_text("uncommitted manager change\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--seed-checkout",
                str(checkout),
                "--release-tag",
                _RELEASE_TAG,
                "--seed-repository",
                _CANONICAL_SEED_REPOSITORY,
                "--seed-profile",
                _CANONICAL_SEED_PROFILE,
                "--manager-repository",
                _MANAGER_REPOSITORY,
                "--manager-release-tag",
                _MANAGER_RELEASE_TAG,
                "--manager-checkout",
                str(checkout),
                "--manager-ref",
                manager_ref,
                "--manager-source-repository",
                _MANAGER_SOURCE_REPOSITORY,
                "--output-root",
                str(output_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        dirty_path.unlink()
    _check(
        result.returncode != 0
        and "--manager-checkout must be clean before staging" in result.stderr,
        f"stage_release.py refuses a dirty manager checkout: {result.stderr}",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        checkout, manager_checkout, manager_ref = _build_seed_fixture(root)
        shared_head_commit = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()
        expected_commit = _run_git(checkout, "rev-parse", _RELEASE_TAG).stdout.strip()
        expected_tree = _run_git(checkout, "rev-parse", f"{_RELEASE_TAG}^{{tree}}").stdout.strip()
        expected_manager_commit = manager_ref
        expected_manager_tree = _run_git(
            manager_checkout, "rev-parse", "HEAD^{tree}"
        ).stdout.strip()
        _check(
            expected_manager_commit != shared_head_commit,
            "shared checkout HEAD differs from the manager source commit",
        )

        output_a = root / "stage-a"
        stage_a = _stage(checkout, manager_checkout, manager_ref, output_a)

        metadata = json.loads((output_a / "release_metadata.json").read_text(encoding="utf-8"))
        _check_metadata(
            metadata,
            expected_commit,
            expected_tree,
            manager_ref,
            expected_manager_commit,
            expected_manager_tree,
        )
        archive_path = _payload_archive_path(output_a)
        formula = (output_a / "Formula" / "solet.rb").read_text(encoding="utf-8")
        real_sha256 = _check_archive(archive_path, metadata, formula)
        _check_contract_digest_implementation_parity(
            checkout,
            _staged_contract_digest(stage_a),
        )
        _check_extracted_contract_bundle(
            archive_path,
            expected_commit,
            _staged_contract_digest(stage_a),
            root,
        )
        _check_rendered_outputs(output_a, real_sha256)
        _check_staged_metadata_is_rerenderable(output_a)
        _check_archive_guard_rejects_pre_fix_omission()

        output_b = root / "stage-b"
        _stage(checkout, manager_checkout, manager_ref, output_b)
        archive_path_b = _payload_archive_path(output_b)
        _check(
            archive_path.read_bytes() == archive_path_b.read_bytes(),
            "staging the same commit twice is byte-for-byte reproducible",
        )

        _check_lock_only(checkout, root / "stage-lock-only", expected_commit, expected_tree)
        _check_refused_invocation(manager_checkout)
        _check_refused_manager_refs(manager_checkout)
        _check_manager_checkout_must_be_pinned(
            checkout, manager_ref, shared_head_commit, root / "unpinned-manager-output"
        )
        _check_manager_checkout_must_be_clean(
            manager_checkout, manager_ref, root / "dirty-manager-output"
        )

    print(f"stage_release_smoke: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
