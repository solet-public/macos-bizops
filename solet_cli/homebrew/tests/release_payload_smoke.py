"""Focused no-network smoke for release rendering and Formula package closure.

The installability checks are host-dependent by design (see the README):
they require `brew` and the Formula-declared `python@3.13`, but touch no
network — no PyPI index, no fetch of the Formula's own pinned
`setuptools`/`wheel` resources. This file only proves the Formula's *text*
(pinned resources declared with sha256, installed before the
build_isolation:false manager step) and a local RED control (setuptools is
absent from a clean venv unless something provisions it). The network half
of that proof — fetching the pinned wheels, verifying them against their
declared checksums, and confirming they actually make the venv importable
with user site-packages excluded — lives in
`ci/resource_provisioning_acceptance.py`, gated like
`ci/lifecycle_acceptance.py`, and is never registered as a gate smoke.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY = _ROOT.parents[1]
_RENDERER = _ROOT / "scripts" / "render_release_payload.py"
_EXAMPLE = _ROOT / "release_metadata.example.json"
_checks = 0

sys.path.insert(0, str(_REPOSITORY / "solet_cli" / "src"))

from solet_manager.cli import run  # noqa: E402


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _run_renderer(
    metadata_path: Path,
    output: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_RENDERER),
            "--metadata",
            str(metadata_path),
            "--output-root",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_metadata(path: Path, metadata: dict[str, object]) -> None:
    path.write_text(json.dumps(metadata), encoding="utf-8")


def _render_valid_payload(
    root: Path,
    metadata: dict[str, object],
) -> tuple[str, dict[str, object], Path]:
    metadata_path = root / "release.json"
    output = root / "output"
    _write_metadata(metadata_path, metadata)
    result = _run_renderer(metadata_path, output)
    _check(result.returncode == 0, f"release payload render failed: {result.stderr}")
    formula = (output / "Formula" / "solet.rb").read_text(encoding="utf-8")
    lock_value: object = json.loads(
        (output / "solet_cli" / "homebrew" / "seed.lock.json").read_text(
            encoding="utf-8"
        )
    )
    _check(isinstance(lock_value, dict), "rendered seed lock is an object")
    return formula, cast(dict[str, object], lock_value), output


def _check_named_seed_catalog(
    output: Path,
    metadata: dict[str, object],
    lock: dict[str, object],
) -> None:
    """The tap symlink exposes this exact path to ``_resolve_named_seed_lock``."""
    profile = str(metadata["seed_profile"])
    catalog_lock = output / "solet_cli" / "homebrew" / "seeds" / profile / "seed.lock.json"
    _check(
        catalog_lock.is_file(),
        "renderer emits the named-seed catalog lock at the resolver's symlink target",
    )
    catalog_value: object = json.loads(catalog_lock.read_text(encoding="utf-8"))
    _check(
        catalog_value == lock,
        "named-seed catalog lock is byte-equivalent JSON to the bundled default lock",
    )


def _check_formula_boundary(
    formula: str,
    lock: dict[str, object],
    metadata: dict[str, object],
) -> None:
    _check_formula_install_shape(formula, lock)
    _check_rendered_identity(formula, lock, metadata)
    brewfile = (_ROOT / "ci" / "Brewfile.enterprise.example").read_text(encoding="utf-8")
    _check(
        'trusted: { formula: "solet" }' in brewfile and "trusted: true" not in brewfile,
        "Brewfile trusts only the formula",
    )


def _check_formula_install_shape(formula: str, lock: dict[str, object]) -> None:
    _check(
        "{{" not in formula and "post_install" not in formula,
        "formula has no markers or post-install mutation",
    )
    _check(
        "def caveats" in formula and "Next: run solet create" in formula,
        "formula prints the standard separate setup instruction",
    )
    _check(
        "include Language::Python::Virtualenv" in formula,
        "formula imports Homebrew's Python virtualenv helpers",
    )
    _check(
        'depends_on "python@3.13"' in formula and 'depends_on "git"' in formula,
        "formula declares runtime dependencies",
    )
    _check(
        "virtualenv_create(libexec" in formula
        and "build_isolation: false" in formula
        and "bin.install_symlink" in formula,
        "formula owns a closed virtualenv and public manager wrapper",
    )
    _check(
        "assert_match" in formula and "list --json" in formula and "create brew-test" in formula,
        "formula test is behavioral",
    )
    _check(
        "brew install" not in formula and "services" not in formula,
        "formula does not create machine or instance state",
    )
    _check_seeds_symlink_shape(formula, lock)


def _check_seeds_symlink_shape(formula: str, lock: dict[str, object]) -> None:
    lock_marker = '(libexec/"share"/"solet"/"seed.lock.json").write <<~JSON\n'
    lock_at = formula.find(lock_marker)
    seeds_at = formula.find(
        'install_symlink Pathname(__dir__).parent/"solet_cli"/"homebrew"/"seeds" => "seeds"'
    )
    _check(lock_at != -1, "formula writes the reviewed default lock inside the sandbox")
    lock_end = formula.find("    JSON\n", lock_at + len(lock_marker))
    embedded: object | None = None
    if lock_at != -1 and lock_end != -1:
        embedded = json.loads(formula[lock_at + len(lock_marker) : lock_end])
    _check(embedded == lock, "formula's embedded default lock matches the rendered lock")
    _check(
        '.install Pathname(__dir__).parent/"solet_cli"/"homebrew"/"seed.lock.json"'
        not in formula,
        "formula does not read the tap checkout inside Homebrew's build sandbox",
    )
    _check(seeds_at != -1, "formula symlinks the discoverable seed set from the tap directory")
    _check(
        lock_at != -1 and seeds_at != -1 and lock_at < seeds_at,
        "seeds symlink is declared after the bundled-default lock install",
    )


def _check_rendered_identity(
    formula: str,
    lock: dict[str, object],
    metadata: dict[str, object],
) -> None:
    _check(
        lock["commit"] == metadata["seed_commit"]
        and lock["repository"] == metadata["seed_repository"]
        and lock["profile"] == metadata["seed_profile"] == "macos-bizops",
        "seed lock carries reviewed identity",
    )
    _check(
        metadata["release_archive_sha256"] == lock["archive_sha256"]
        and str(metadata["release_archive_sha256"]) in formula,
        "one sealed archive checksum drives formula and seed lock",
    )
    _check(
        str(metadata["manager_url"]) in formula,
        "formula carries the independently validated manager release URL",
    )
    _check(
        "solet-public/homebrew-tap" in str(metadata["manager_url"])
        and "https://github.com/solet-public/macos-bizops.git"
        == metadata["seed_repository"],
        "public manager host and seed repository are independent identities",
    )


def _expect_refused(
    root: Path,
    metadata: dict[str, object],
    label: str,
    expected_error: str,
) -> None:
    metadata_path = root / f"{label}.json"
    output = root / f"output-{label}"
    _write_metadata(metadata_path, metadata)
    result = _run_renderer(metadata_path, output)
    _check(
        result.returncode != 0 and expected_error in result.stderr,
        f"{label} is refused: {result.stderr}",
    )
    _check(
        not (output / "Formula" / "solet.rb").exists()
        and not (output / "solet_cli" / "homebrew" / "seed.lock.json").exists(),
        f"{label} writes no renderer outputs",
    )


def _check_identity_refusals(root: Path, metadata: dict[str, object]) -> None:
    dual_checksum = dict(metadata)
    dual_checksum["manager_sha256"] = "d" * 64
    _expect_refused(root, dual_checksum, "dual-checksum", "must contain exactly")

    cases: tuple[tuple[str, str, str, str], ...] = (
        (
            "latest-route",
            "manager_url",
            "https://github.com/solet-public/macos-bizops/releases/latest/download/solet-0.1.0.tar.gz",
            "uploaded release asset",
        ),
        ("moving-tag", "seed_release_tag", "LaTeSt", "invalid immutable identity"),
        ("mixed-case-main", "seed_release_tag", "MaIn", "invalid immutable identity"),
        (
            "moving-manager-tag",
            "manager_url",
            "https://github.com/solet-public/homebrew-tap/releases/download/main/solet-0.1.0.tar.gz",
            "invalid immutable identity",
        ),
        (
            "query-ambiguity",
            "manager_url",
            str(metadata["manager_url"]) + "?download=1",
            "unambiguous",
        ),
        (
            "fragment-ambiguity",
            "manager_url",
            str(metadata["manager_url"]) + "#asset",
            "unambiguous",
        ),
        (
            "unversioned-asset",
            "manager_url",
            "https://github.com/solet-public/macos-bizops/releases/download/release-2026-08-21/solet.tar.gz",
            "versioned archive",
        ),
    )
    for label, key, value, expected_error in cases:
        changed = dict(metadata)
        changed[key] = value
        _expect_refused(root, changed, label, expected_error)

    # Manager distribution identity is independent of seed identity: the
    # public release asset can live in the tap while the macos-bizops seed
    # remains in its own repository with its immutable tag/commit/tree tuple.


def _check_independent_manager_identities(
    root: Path,
    metadata: dict[str, object],
) -> None:
    cases = (
        (
            "independent-manager-tag",
            "https://github.com/solet-public/macos-bizops/releases/download/"
            "release-OTHER/solet-0.1.0.tar.gz",
        ),
        (
            "independent-manager-repository",
            "https://github.com/solet-public/other/releases/download/"
            "release-2026-08-21/solet-0.1.0.tar.gz",
        ),
    )
    for label, manager_url in cases:
        case_root = root / label
        case_root.mkdir()
        changed = dict(metadata)
        changed["manager_url"] = manager_url
        formula, _, _ = _render_valid_payload(case_root, changed)
        _check(manager_url in formula, f"{label} is carried into the Formula")


def _homebrew_python() -> Path:
    brew = shutil.which("brew")
    if brew is None:
        # Exit 77 is the register's dedicated environment-skip code: a host
        # (or a constrained gate PATH) without Homebrew cannot exercise the
        # formula-install leg at all, and that is an environment fact about
        # the runner, not a defect in the payload under test.
        print(
            "SKIP  formula-install checks: no `brew` on PATH to provide the "
            "declared python@3.13 dependency"
        )
        raise SystemExit(77)
    result = subprocess.run(
        [brew, "--prefix", "python@3.13"],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(
        result.returncode == 0,
        "host prerequisite missing: install the Formula-declared Homebrew "
        f"dependency with `brew install python@3.13`: {result.stderr.strip()}",
    )
    python = Path(result.stdout.strip()) / "bin" / "python3.13"
    _check(
        python.is_file(),
        "host prerequisite missing: the Formula-declared Homebrew python@3.13 "
        f"executable is absent: {python}",
    )
    return python


_RESOURCE_BLOCK = re.compile(
    r'resource\s+"(?P<name>[^"]+)"\s+do\s+'
    r'url\s+"(?P<url>[^"]+)"\s+'
    r'sha256\s+"(?P<sha256>[0-9a-f]{64})"\s+'
    r"end",
)


def _parse_pinned_resources(formula: str) -> dict[str, tuple[str, str]]:
    """Every ``resource "name" do url ... sha256 ... end`` block, by name."""
    return {
        match.group("name"): (match.group("url"), match.group("sha256"))
        for match in _RESOURCE_BLOCK.finditer(formula)
    }


def _assert_resources_declared_before_manager_install(formula: str) -> None:
    """Offline ordering check: ``pip_install resources`` must precede the
    ``build_isolation: false`` manager install, or the resources exist in
    the Formula without ever provisioning the venv before it needs them.
    """
    resources_at = formula.find("pip_install resources")
    contracts_at = formula.find('pip_install buildpath/"solet_setup_contracts"')
    manager_at = formula.find('pip_install buildpath/"solet_cli"')
    _check(resources_at != -1, "Formula installs the declared resources into the venv")
    _check(
        contracts_at != -1,
        "Formula installs shared setup contracts with build_isolation: false",
    )
    _check(manager_at != -1, "Formula installs the manager with build_isolation: false")
    _check(
        resources_at != -1 and contracts_at != -1 and manager_at != -1
        and resources_at < contracts_at < manager_at,
        "resources install before shared contracts, which install before the manager",
    )


def _no_user_site_env() -> dict[str, str]:
    """Strip ambient per-user site-packages so a probe measures only what
    Homebrew's python@3.13 + this Formula's own resources actually provide —
    never what this machine happens to have accumulated in
    ``~/Library/Python`` or equivalent. Without this, the probe can pass on
    a real machine only because of an unrelated personal
    `pip install --user setuptools`, not because of anything Homebrew or
    this Formula guarantees.
    """
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _check_no_index_formula_install(root: Path, formula: str) -> None:
    python = _homebrew_python()
    venv = root / "formula-venv"
    created = subprocess.run(
        [
            str(python),
            "-m",
            "venv",
            "--system-site-packages",
            "--without-pip",
            str(venv),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(created.returncode == 0, f"Homebrew Python virtualenv failed: {created.stderr}")
    venv_python = venv / "bin" / "python3.13"
    no_user_site = _no_user_site_env()

    # RED, unconditionally, every run: with ambient per-user state excluded,
    # a bare --system-site-packages venv against Homebrew's own python@3.13
    # must NOT already have setuptools — if this ever passes, either Homebrew
    # started shipping it (drop the resources below) or this probe stopped
    # measuring what it claims to.
    before = subprocess.run(
        [str(venv_python), "-c", "import setuptools"],
        check=False,
        capture_output=True,
        text=True,
        env=no_user_site,
    )
    _check(
        before.returncode != 0,
        "RED control: setuptools is absent from a clean venv before the "
        "pinned resources are installed (if this is green, the probe is "
        "not measuring what this Formula actually provides)",
    )

    resources = _parse_pinned_resources(formula)
    _check(
        {"setuptools", "wheel", "packaging"} <= resources.keys(),
        "Formula declares pinned setuptools, wheel, and packaging resources",
    )
    _assert_resources_declared_before_manager_install(formula)

    # The GREEN half of this proof — fetching the pinned wheels, verifying
    # their checksums, --no-index installing them, and re-checking the
    # PYTHONNOUSERSITE=1 import — needs the network and therefore cannot be
    # a gate-registered smoke (this file must pass fully offline). That half
    # lives in ci/resource_provisioning_acceptance.py, gated the same way
    # solet_cli/homebrew/ci/lifecycle_acceptance.py gates its own real,
    # network-dependent run. The RED control above is what stays here.

    environment = os.environ.copy()
    environment.update({"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    installed = subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-binary=:all:",
            "--ignore-installed",
            "--no-compile",
            "--no-build-isolation",
            str(_REPOSITORY / "solet_setup_contracts"),
            str(_REPOSITORY / "solet_cli"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    _check(installed.returncode == 0, f"no-index manager install failed: {installed.stderr}")
    version = subprocess.run(
        [str(venv / "bin" / "solet"), "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    _check(
        version.returncode == 0 and version.stdout.strip() == "solet 0.1.0",
        "installed no-index manager entrypoint runs",
    )


def _check_packaged_flow_dry_run(root: Path) -> None:
    seed_lock = root / "seed.lock.json"
    seed_lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "https://github.com/solet-public/macos-bizops.git",
                "release_tag": "release-2026-08-21",
                "commit": "a" * 40,
                "tree_hash": "b" * 40,
                "archive_sha256": "c" * 64,
                "profile": "macos-bizops",
            }
        ),
        encoding="utf-8",
    )
    target = root / "Solets" / "brew-test"
    result = run(
        [
            "--home",
            str(root / "manager"),
            "--contract-dir",
            str(_REPOSITORY / "plugins" / "github_midwife_plugin" / "knowledge_base"),
            "--seed-lock",
            str(seed_lock),
            "create",
            "brew-test",
            "--target",
            str(target),
            "--decision",
            "inference_implementation=none",
            "--decision",
            "execution_topology=solo",
            "--decision",
            "git_mutation_control=single_session",
            "--decision",
            "session_sources=",
            "--dry-run",
            "--json",
        ]
    )
    _check(
        result.kind == "create_preview"
        and result.status == "preview_ready"
        and int(result.exit_code) == 0,
        "packaged macos-bizops flow reaches a successful dry-run frontier",
    )
    _check(
        result.data.get("dry_run_writes") == 0 and not target.exists(),
        "dry-run writes no target state",
    )


def main() -> int:
    metadata_value: object = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    _check(isinstance(metadata_value, dict), "example release metadata is an object")
    metadata = cast(dict[str, object], metadata_value)
    metadata["manager_url"] = (
        "https://github.com/solet-public/homebrew-tap/releases/download/"
        "manager-v0.1.0-r0/solet-0.1.0.tar.gz"
    )
    metadata["manager_source_ref"] = "a" * 40
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        formula, lock, output = _render_valid_payload(root, metadata)
        _check_formula_boundary(formula, lock, metadata)
        _check_named_seed_catalog(output, metadata, lock)
        _check_identity_refusals(root, metadata)
        _check_independent_manager_identities(root, metadata)
        _check_no_index_formula_install(root, formula)
        _check_packaged_flow_dry_run(root)
    print(f"release_payload_smoke OK: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
