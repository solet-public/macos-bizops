"""Stage a Homebrew manager payload and an independently identified seed lock.

Builds the deterministic manager-payload archive (the public-distribution
``LICENSE`` and ``NOTICE``, ``solet_cli/``, and the birth-spine setup
contracts), computes its checksum, resolves the seed's own git identity at the
given ref, records the manager source identity, writes ``release_metadata.json``
and a draft ``release_manifest.json`` (schema v1 per the `publish_release`
design §7.1 — seed and manager sections only; the remaining sections are
finalised by later stages of that verb, out of this script's job), and then
calls the existing ``render_release_payload.py`` to produce the real
``Formula/solet.rb`` and ``solet_cli/homebrew/seed.lock.json`` -- the Formula
also installs this same draft into the keg as
``share/solet/release_manifest.json``, which is what lets the consumption-side
pairing gate (``solet_manager.release_identity_gate``) read it (iss_18c47206).

Deliberately does not touch git, GitHub, or any credential: it reads a
worktree that has already been checked out (by the workflow, or by hand for
local testing) and writes only under ``--output-root``. Nothing here creates
a tap, cuts a release, or uploads an asset — those remain separate,
explicitly authorized acts outside this script's job.

The payload excludes ``solet_cli/homebrew/seed.lock.json`` deliberately: the
rendered lock's own ``archive_sha256`` field names this payload archive's
checksum, so the archive cannot also contain that field's value without
being self-referential. The Formula template embeds the same rendered lock
fields and writes them inside Homebrew's build sandbox, rather than reading
the tap checkout or the downloaded payload. This keeps the checksum binding
non-circular without crossing the sandbox boundary.

``stage()`` is the importable entry point the `publish_release` verb calls
in-process (sealed repo as ``seed_checkout``, minting checkout at
``source_ref`` as ``manager_checkout``); ``main()`` is a thin CLI wrapper
over it. ``stage()`` re-validates its own inputs rather than trusting a
caller, since it is a public function, not just an argparse target.

What the archive must contain is DERIVED, not listed (iss_686b2564,
iss_0f999f9e). ``solet_setup_contracts.payload_requirements`` reads the
manager commit's own shipped Python sources and reports every path rooted at
``sys.prefix``, ``__file__`` or ``importlib.resources``; this script is the
manager adapter that maps each such leaf to the archive member that provides
it, through the Formula's own install statements and each distribution's
``package-data``. The Formula is a reader too: what it ``pip_install``s and
installs from the archive is required. Every required leaf must be a regular
member of the archive before the archive is written; an existing parent
directory never satisfies a leaf. A reader the derivation cannot finish, or a
read of a derived leaf through a path that is not production-anchored, is
printed as an advisory and never counted as coverage. The helper is loaded by
path from THIS checkout, the same commit whose ``stage_release.py`` is
running, never from an installed copy.
"""

from __future__ import annotations

import argparse
import ast
import functools
import gzip
import hashlib
import importlib.util
import io
import json
import posixpath
import re
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from solet_setup_contracts.payload_requirements import (
        AnchoredRead,
        Finding,
        MirrorRecord,
        ReaderCensus,
        Requirement,
    )

_GIT_TIMEOUT_S = 30
_CHECKOUT_ROOT = Path(__file__).resolve().parents[3]
_FORMULA_TEMPLATE_RELATIVE = "solet_cli/homebrew/Formula/solet.rb.template"
_PAYLOAD_REQUIREMENTS_SOURCE = _CHECKOUT_ROOT / "solet_setup_contracts" / "src" / "solet_setup_contracts" / "payload_requirements.py"
_PAYLOAD_REQUIREMENTS_MODULE = "solet_stage_release_payload_requirements"
_CONTRACTS_INSTALL_DIRECTORY = "share/solet/contracts"
_SITE_PACKAGES = "site-packages"
_POLICY_LEAVES = {
    "LICENSE": "public-distribution license",
    "NOTICE": "public-distribution notice",
}

# The Formula statements that place archive members (or rendered bytes) below
# ``libexec``, which is the manager venv's ``sys.prefix``.
_FORMULA_DESTINATION = r'\(libexec(?P<dest>(?:/"[^"]+")*)\)'
_FORMULA_INSTALL_DIR = re.compile(_FORMULA_DESTINATION + r"\.install Dir\[(?P<body>[^\]]*)\]")
_FORMULA_WRITE = re.compile(_FORMULA_DESTINATION + r"\.write\b")
_FORMULA_LINK = re.compile(_FORMULA_DESTINATION + r'\.install_symlink [^\n]*=> "(?P<name>[^"]+)"')
_FORMULA_PIP = re.compile(r'venv\.pip_install buildpath/"(?P<path>[^"]+)"')
_QUOTED = re.compile(r'"([^"]+)"')
_FORMULA_INSTALL = "install"
_FORMULA_RENDERED = "rendered"
_FORMULA_LINKED = "linked"


@functools.cache
def _payload_requirements() -> ModuleType:
    """Load the stdlib derivation helper from this checkout by file path, once.

    By path, not by import: in-process callers (``release_world``) already
    hold an imported ``solet_setup_contracts`` from the RUNNING release, which
    can predate the commit being staged. The dataclasses in the helper need
    their module registered before it executes.
    """
    spec = importlib.util.spec_from_file_location(_PAYLOAD_REQUIREMENTS_MODULE, _PAYLOAD_REQUIREMENTS_SOURCE)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the payload requirement helper at {_PAYLOAD_REQUIREMENTS_SOURCE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class _FormulaEntry:
    """One path the Formula puts below ``sys.prefix``, and from where."""

    installed_path: str
    source: str | None
    kind: str
    line: int


@dataclass(frozen=True)
class _FormulaLayout:
    """The installer's own map from archive members to installed paths."""

    path: str
    entries: tuple[_FormulaEntry, ...]
    pip_targets: tuple[tuple[str, int], ...]

    def entry(self, installed_path: str) -> _FormulaEntry | None:
        """The statement that produces exactly ``installed_path``, if any.

        An install, a rendered ``write`` or a symlink: each is a different
        coverage, and only an install reads from the archive.
        """
        return next((item for item in self.entries if item.installed_path == installed_path), None)

    def holds_below(self, installed_path: str) -> bool:
        """Whether some statement installs below ``installed_path``.

        True for an installed DIRECTORY such as ``share/solet/contracts``: a
        read of the directory itself is a reference, not a leaf requirement.
        """
        return any(item.installed_path.startswith(f"{installed_path}/") for item in self.entries)

    def install_sources(self, installed_directory: str) -> tuple[str, ...]:
        """Archive sources installed into one directory, in Formula order.

        Also how an unlisted leaf below that directory is mapped to the one
        source directory the Formula fills it from.
        """
        installs = (item for item in self.entries if item.kind == _FORMULA_INSTALL and item.source is not None)
        return tuple(cast(str, item.source) for item in installs if posixpath.dirname(item.installed_path) == installed_directory)

    def install_line(self, installed_directory: str) -> int:
        """The first Formula line that installs into ``installed_directory``.

        Named in the evidence for a leaf the Formula would have to list.
        """
        return min(item.line for item in self.entries if posixpath.dirname(item.installed_path) == installed_directory)


def _parse_formula(text: str, path: str) -> _FormulaLayout:
    """Read the install, write, symlink and ``pip_install`` statements of a Formula.

    A Formula that installs no payload distribution cannot be mapped at all,
    so it refuses; the shipped template always names two.
    """
    entries: list[_FormulaEntry] = []
    for match in _FORMULA_INSTALL_DIR.finditer(text):
        destination = "/".join(_QUOTED.findall(match.group("dest")))
        for item in _QUOTED.finditer(match.group("body")):
            line = text.count("\n", 0, match.start("body") + item.start()) + 1
            entries.append(_FormulaEntry(posixpath.join(destination, posixpath.basename(item.group(1))), item.group(1), _FORMULA_INSTALL, line))
    for match in _FORMULA_WRITE.finditer(text):
        entries.append(_FormulaEntry("/".join(_QUOTED.findall(match.group("dest"))), None, _FORMULA_RENDERED, text.count("\n", 0, match.start()) + 1))
    for match in _FORMULA_LINK.finditer(text):
        installed = posixpath.join("/".join(_QUOTED.findall(match.group("dest"))), match.group("name"))
        entries.append(_FormulaEntry(installed, None, _FORMULA_LINKED, text.count("\n", 0, match.start()) + 1))
    pip_targets = tuple((match.group("path"), text.count("\n", 0, match.start()) + 1) for match in _FORMULA_PIP.finditer(text))
    if not pip_targets:
        raise SystemExit(f"{path} installs no payload distribution; its payload cannot be mapped")
    return _FormulaLayout(path=path, entries=tuple(entries), pip_targets=pip_targets)


_FORMULA_LAYOUT = _parse_formula((_CHECKOUT_ROOT / _FORMULA_TEMPLATE_RELATIVE).read_text(encoding="utf-8"), _FORMULA_TEMPLATE_RELATIVE)

_PAYLOAD_SOURCE_PATHS = ("LICENSE", "NOTICE", "solet_setup_contracts", "solet_cli")
# The contract files the Formula installs into share/solet/contracts, read
# from the Formula's own install statement rather than kept as a second list.
# The derivation already archives them; this projection exists for staging
# fixtures that must reproduce what the payload carries
# (quality_gates/tests/release_workflow_parity_smoke.py).
_CONTRACT_PATHS: tuple[str, ...] = _FORMULA_LAYOUT.install_sources(_CONTRACTS_INSTALL_DIRECTORY)
# The closed existing-install transition bundle (design section 2.3): digested
# with the same filename/NUL/bytes/NUL discipline as the create bundle, over a
# DISTINCT file set that never includes macos_setup_flow.json.  Its digest is
# what seed-lock v3 binds as existing_install_contract.bundle_digest.
_TRANSITION_BUNDLE_PATHS = (
    "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json",
    "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_adapter_envelope.schema.json",
)
_DIGESTED_CONTRACT_PATHS = (
    "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_flow.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_answers.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_journal.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_adapter_envelope.schema.json",
)
_EXCLUDED_FROM_PAYLOAD = "solet_cli/homebrew/seed.lock.json"
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MOVING_RELEASE_NAMES = frozenset({"head", "main", "master", "latest"})
_PROFILE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_SEED_REPOSITORY = re.compile(
    r"^https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
    r"/[A-Za-z0-9_.-]{1,100}\.git$"
)
# Release-label discriminator ("r<NN>"): folded into the asset filename so two
# releases of the same manager version never share a name (closes
# iss_ba7f7103 together with the version-agreement check below).
_RELEASE_LABEL = re.compile(r"^r[0-9]+$")
_MANAGER_MODELS_RELATIVE_PATH = Path("solet_cli") / "src" / "solet_manager" / "models.py"
_RELEASE_MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class _GitIdentity:
    commit: str
    tree_hash: str


@dataclass(frozen=True)
class StageResult:
    """Everything a caller (the CLI, or `publish_release` in-process) needs
    back from a completed stage."""

    output_root: Path
    payload_path: Path
    payload_sha256: str
    metadata_path: Path
    manifest_path: Path
    manifest: dict[str, object]
    formula_path: Path
    lock_path: Path
    install_mode: str
    manager_commit: str
    contract_digest: str
    transition_digest: str
    payload_requirements: PayloadEvidence
    payload_mirrors: tuple[MirrorRecord, ...]


def main() -> int:
    args = _parser().parse_args()
    _require_tag(args.release_tag)
    _require_seed_repository(args.seed_repository)
    _require_profile(args.seed_profile)
    seed_checkout = args.seed_checkout.resolve()
    output_root = _output_root(args.output_root, seed_checkout)

    if args.lock_only:
        return _emit_lock_only(seed_checkout, output_root, args)

    if args.manager_checkout is None:
        raise SystemExit("--manager-checkout is required unless --lock-only is used")
    manager_checkout = args.manager_checkout.resolve()
    output_root = _output_root(args.output_root, seed_checkout, manager_checkout)

    result = stage(
        seed_checkout=seed_checkout,
        release_tag=args.release_tag,
        seed_repository=args.seed_repository,
        seed_profile=args.seed_profile,
        seed_channel_id=args.seed_channel_id,
        manager_checkout=manager_checkout,
        manager_ref=args.manager_ref,
        manager_source_repository=args.manager_source_repository,
        manager_repository=args.manager_repository,
        manager_release_tag=args.manager_release_tag,
        formula_revision=args.formula_revision,
        dev_mode=args.dev_mode,
        output_root=output_root,
        release_label=args.release_label,
        allow_manager_seed_skew=args.allow_manager_seed_skew,
    )

    print(f"payload archive:  {result.payload_path} ({result.payload_sha256})")
    print(f"release metadata: {result.metadata_path}")
    print(f"release manifest: {result.manifest_path}")
    print(f"rendered formula: {result.formula_path}")
    print(f"rendered lock:    {result.lock_path}")
    print(f"install mode:     {result.install_mode} ({result.manager_commit})")
    print(f"setup contracts:  {result.contract_digest} (manager payload == seed artifact)")
    print(f"transition bundle: {result.transition_digest} (seed artifact existing-install)")
    _print_payload_evidence(result.payload_requirements, result.payload_mirrors)
    print("Nothing was pushed, tagged, released, or uploaded.")
    return 0


def _print_payload_evidence(derivation: PayloadEvidence, mirrors: tuple[MirrorRecord, ...]) -> None:
    """Every derived requirement, advisory and bundle mirror, one line each."""
    print(f"payload requirements: {len(derivation.requirements)} derived from {len(derivation.modules)} shipped modules and the Formula")
    for requirement in derivation.requirements:
        print(f"payload requirement: {requirement.describe()}")
    for finding in derivation.advisories:
        print(f"payload advisory: {finding.kind} at {finding.location}: {finding.reason} ({finding.expression})")
    for mirror in mirrors:
        label = "canonical" if mirror.canonical else "mirror"
        missing = ", ".join(mirror.missing) or "none"
        print(f"payload {label}: {mirror.directory} carries {len(mirror.present)} bundle leaves, missing: {missing}")


def stage(
    *,
    seed_checkout: Path,
    release_tag: str,
    seed_repository: str,
    seed_profile: str,
    seed_channel_id: str,
    manager_checkout: Path,
    manager_ref: str | None,
    manager_source_repository: str | None,
    manager_repository: str | None,
    manager_release_tag: str | None,
    formula_revision: int,
    dev_mode: bool,
    output_root: Path,
    release_label: str | None = None,
    allow_manager_seed_skew: str | None = None,
) -> StageResult:
    """Stage a manager payload + seed lock pair (design §5.4 stage 5).

    Called by the CLI (thin wrapper, see `main()`) and, in-process, by the
    `publish_release` verb with the sealed repo as `seed_checkout` and the
    minting checkout at `source_ref` as `manager_checkout`. Re-validates every
    input itself: a public function must not trust its caller's argparse
    already having done so.
    """
    _require_tag(release_tag)
    _require_seed_repository(seed_repository)
    _require_profile(seed_profile)
    manager_repository_value = None if dev_mode else _require_manager_repository(manager_repository)
    manager_release_tag_value = None if dev_mode else _require_manager_release_tag(manager_release_tag)
    manager_ref_value = _require_manager_ref(manager_ref)
    manager_source_repository_value = _require_manager_source_repository(manager_source_repository)
    if release_label is not None:
        _require_release_label(release_label)
    seed_identity = _resolve_identity(seed_checkout, release_tag, "seed release tag")
    seed_provenance = _seed_provenance_at_commit(seed_checkout, seed_identity.commit)
    manager_identity = _resolve_identity(manager_checkout, manager_ref_value, "manager ref")
    _require_manager_checkout_pinned(manager_checkout, manager_identity, manager_ref_value)
    _require_manager_checkout_clean(manager_checkout)
    derivation = _derive_payload_requirements(manager_checkout, manager_identity.commit)
    version = _read_manager_version(manager_checkout)
    _require_manager_version_agreement(manager_checkout, version)
    contract_digest = _require_contract_pair(
        manager_checkout=manager_checkout,
        manager_commit=manager_identity.commit,
        seed_checkout=seed_checkout,
        seed_commit=seed_identity.commit,
    )
    transition_digest = _require_transition_bundle(seed_checkout, seed_identity.commit)
    _require_same_source_revision(
        manager_source_commit=manager_identity.commit,
        seed_provenance_source_commit=seed_provenance["source_commit"],
        allow_manager_seed_skew=allow_manager_seed_skew,
    )
    asset_name = f"solet-{version}.tar.gz" if release_label is None else f"solet-{version}-{release_label}.tar.gz"

    payload_dir = output_root / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_path = payload_dir / asset_name
    payload_sha256, file_digests, derivation, mirrors = _build_payload_archive(manager_checkout, manager_identity.commit, payload_path, derivation)

    install_mode = "dev" if dev_mode else "release"
    manager_url = payload_path.as_uri() if dev_mode else (f"https://github.com/{_owner_repo(_require_value(manager_repository_value))}/releases/download/{_require_value(manager_release_tag_value)}/{asset_name}")
    metadata = {
        "formula_revision": formula_revision,
        "install_mode": install_mode,
        "manager_url": manager_url,
        "manager_source_repository": manager_source_repository_value,
        "manager_source_ref": manager_ref_value,
        "manager_source_commit": manager_identity.commit,
        "manager_source_tree_hash": manager_identity.tree_hash,
        "release_archive_sha256": payload_sha256,
        "seed_repository": seed_repository,
        "seed_release_tag": release_tag,
        "seed_commit": seed_identity.commit,
        "seed_tree_hash": seed_identity.tree_hash,
        "seed_profile": seed_profile,
        "seed_channel_id": seed_channel_id,
        "seed_provenance": seed_provenance,
        "existing_install_contract": {
            "flow_id": "existing-install",
            "flow_schema_version": 1,
            "bundle_digest": transition_digest,
        },
        "allowed_repository_migrations": [],
    }
    metadata_path = output_root / "release_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    manifest = _build_release_manifest_draft(
        release_label=release_label,
        manager_release_tag=manager_release_tag_value,
        seed_repository=seed_repository,
        seed_release_tag=release_tag,
        seed_identity=seed_identity,
        seed_profile=seed_profile,
        seed_channel_id=seed_channel_id,
        seed_provenance=seed_provenance,
        manager_source_repository=manager_source_repository_value,
        manager_identity=manager_identity,
        version=version,
        asset_name=asset_name,
        manager_url=manager_url,
        payload_sha256=payload_sha256,
        file_digests=file_digests,
        contract_digest=contract_digest,
        transition_digest=transition_digest,
        allow_manager_seed_skew=allow_manager_seed_skew,
    )
    manifest_path = output_root / "release_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    _render(metadata_path, manifest_path, output_root)

    return StageResult(
        output_root=output_root,
        payload_path=payload_path,
        payload_sha256=payload_sha256,
        metadata_path=metadata_path,
        manifest_path=manifest_path,
        manifest=manifest,
        formula_path=output_root / "Formula" / "solet.rb",
        lock_path=output_root / "solet_cli" / "homebrew" / "seed.lock.json",
        install_mode=install_mode,
        manager_commit=manager_identity.commit,
        contract_digest=contract_digest,
        transition_digest=transition_digest,
        payload_requirements=derivation,
        payload_mirrors=mirrors,
    )


def _emit_lock_only(checkout: Path, output_root: Path, args: argparse.Namespace) -> int:
    """Bare seed.lock.json for a seed that ships no Homebrew payload of its
    own — no manager archive, no Formula. `archive_sha256` is OMITTED, not
    null: absence means "this seed has no separate payload archive," never
    "unknown" or "unverified" — the commit/tree-hash check below is what
    actually protects the seed's content, independent of this field.
    """
    identity = _resolve_identity(checkout, args.release_tag, "seed release tag")
    lock = {
        "schema_version": 1,
        "repository": args.seed_repository,
        "release_tag": args.release_tag,
        "commit": identity.commit,
        "tree_hash": identity.tree_hash,
        "profile": args.seed_profile,
    }
    output_path = output_root / "seed.lock.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(f"seed lock: {output_path}")
    print("Nothing was pushed, tagged, released, or uploaded.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed-checkout",
        type=Path,
        required=True,
        help="path to a working tree already checked out at --release-tag",
    )
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--seed-repository", required=True, help="HTTPS GitHub .git URL")
    parser.add_argument("--seed-profile", required=True)
    parser.add_argument("--seed-channel-id", default="stable")
    parser.add_argument(
        "--manager-repository",
        help="public GitHub HTTPS .git URL that will host the manager asset",
    )
    parser.add_argument(
        "--manager-release-tag",
        help="immutable manager-specific release tag that will host the asset",
    )
    parser.add_argument(
        "--manager-checkout",
        type=Path,
        help="path to the manager source tree whose bytes will be archived",
    )
    parser.add_argument(
        "--manager-ref",
        help="immutable manager source commit; --manager-checkout must be pinned to it",
    )
    parser.add_argument(
        "--manager-source-repository",
        help="HTTPS GitHub .git URL identifying the manager source tree",
    )
    parser.add_argument("--formula-revision", type=int, default=0)
    parser.add_argument(
        "--dev-mode",
        action="store_true",
        help=("render a Formula overlay whose URL is the staged local payload and whose installed receipt is explicitly dev-mode; never use this for published-pair acceptance"),
    )
    parser.add_argument(
        "--lock-only",
        action="store_true",
        help=("emit a bare seed.lock.json only — no payload archive, no Formula. For an additional seed that ships no Homebrew asset of its own; the checkout need not contain solet_cli/ or the manager contracts."),
    )
    parser.add_argument(
        "--output-root",
        required=True,
        help="non-empty path outside the seed and manager source trees",
    )
    parser.add_argument(
        "--release-label",
        help=("release discriminator 'r<NN>' folded into the payload asset filename (solet-<version>-<label>.tar.gz) so two releases never share a name; omit only for a legacy caller that still expects the undiscriminated name"),
    )
    parser.add_argument(
        "--allow-manager-seed-skew",
        metavar="REASON",
        help=("explicit override recorded on the release manifest: stage anyway when the manager source commit and the seed's PROVENANCE.source_commit differ"),
    )
    return parser


def _require_tag(tag: str) -> None:
    if _TAG.fullmatch(tag) is None:
        raise SystemExit(f"--release-tag has an invalid immutable identity shape: {tag!r}")


def _require_seed_repository(repository: str) -> None:
    if _SEED_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit(f"--seed-repository must be an explicit GitHub HTTPS .git URL: {repository!r}")


def _require_manager_repository(repository: str | None) -> str:
    if repository is None or _SEED_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit("--manager-repository must be an explicit GitHub HTTPS .git URL")
    return repository


def _require_manager_source_repository(repository: str | None) -> str:
    if repository is None or _SEED_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit("--manager-source-repository must be an explicit GitHub HTTPS .git URL")
    return repository


def _require_release_label(label: str) -> None:
    if _RELEASE_LABEL.fullmatch(label) is None:
        raise SystemExit(f"--release-label has an invalid shape (expected 'r<NN>'): {label!r}")


def _read_manager_declared_version(checkout: Path) -> str:
    """Parse `MANAGER_VERSION` out of `solet_manager/models.py`'s source via
    `ast`, never by importing the package — this script stays free of any
    runtime dependency on `solet-manager`/`ananta`."""
    models_path = checkout / _MANAGER_MODELS_RELATIVE_PATH
    try:
        source = models_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read {models_path} to verify MANAGER_VERSION: {exc}") from exc
    tree = ast.parse(source, filename=str(models_path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "MANAGER_VERSION" and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise SystemExit(f"{models_path} has no module-level MANAGER_VERSION string constant")


def _require_manager_version_agreement(checkout: Path, pyproject_version: str) -> None:
    """Refuse a manager checkout where the declared runtime version
    (`solet_manager.models.MANAGER_VERSION`) and the packaged version
    (`solet_cli/pyproject.toml`'s `[project].version`) have drifted apart —
    closes iss_ba7f7103 at the point of use."""
    declared = _read_manager_declared_version(checkout)
    if declared != pyproject_version:
        raise SystemExit(f"manager version disagreement: solet_manager.models.MANAGER_VERSION {declared!r} != solet_cli/pyproject.toml [project].version {pyproject_version!r} (iss_ba7f7103 — the two must move together)")


def _require_same_source_revision(
    *,
    manager_source_commit: str,
    seed_provenance_source_commit: object,
    allow_manager_seed_skew: str | None,
) -> None:
    """Refuse to pair a manager payload with a seed minted from a different
    source commit — closes iss_da99a951. The only way past it is an explicit,
    recorded `--allow-manager-seed-skew <reason>`."""
    if manager_source_commit == seed_provenance_source_commit:
        return
    if allow_manager_seed_skew:
        return
    raise SystemExit(f"manager/seed revision skew: manager source commit {manager_source_commit!r} != seed PROVENANCE.source_commit {seed_provenance_source_commit!r}; pass --allow-manager-seed-skew <reason> to record an explicit override")


def _require_manager_release_tag(tag: str | None) -> str:
    if tag is None or _TAG.fullmatch(tag) is None or tag.casefold() in _MOVING_RELEASE_NAMES:
        raise SystemExit("--manager-release-tag has an invalid immutable identity shape")
    return tag


def _require_manager_ref(ref: str | None) -> str:
    if ref is None or _COMMIT.fullmatch(ref) is None:
        raise SystemExit("--manager-ref has an invalid immutable identity shape")
    return ref


def _require_profile(profile: str) -> None:
    if _PROFILE.fullmatch(profile) is None:
        raise SystemExit(f"--seed-profile has an invalid identity shape: {profile!r}")


def _seed_provenance_at_commit(checkout: Path, commit: str) -> dict[str, object]:
    """Read descriptor facts only from the pinned seed commit, never its worktree."""
    content = _git_blob(checkout, commit, "PROVENANCE.json", "seed artifact")
    try:
        raw: object = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"seed artifact commit {commit} has malformed PROVENANCE.json: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("seed artifact PROVENANCE.json must be an object")
    bundle = raw.get("bundle")
    required = frozenset(
        {
            "schema_version",
            "seed_id",
            "origin_id",
            "source_commit",
            "manifest_sha256",
            "bundle",
            "source_date",
            "lineage",
            "ancestry",
            "signature",
        }
    )
    if frozenset(raw) != required or not isinstance(bundle, dict):
        raise SystemExit("seed artifact PROVENANCE.json does not match the closed v1 shape")
    try:
        return {
            "schema_version": 1,
            "provenance_sha256": hashlib.sha256(content).hexdigest(),
            "seed_id": raw["seed_id"],
            "origin_id": raw["origin_id"],
            "manifest_sha256": raw["manifest_sha256"],
            "bundle_name": bundle["name"],
            "platform": bundle["platform"],
            "source_commit": raw["source_commit"],
            "source_date": raw["source_date"],
        }
    except KeyError as exc:
        raise SystemExit("seed artifact PROVENANCE.json lacks a required descriptor fact") from exc


def _owner_repo(seed_repository: str) -> str:
    return seed_repository.removeprefix("https://github.com/").removesuffix(".git")


def _require_value(value: str | None) -> str:
    """Narrow an already validated release-only CLI value for the renderer."""
    if value is None:
        raise AssertionError("release-only argument was not validated")
    return value


@dataclass(frozen=True)
class _Distribution:
    """One distribution the Formula pip-installs: its package roots and its package data."""

    root: str
    source_roots: tuple[str, ...]
    package_data: tuple[tuple[str, tuple[str, ...]], ...]

    def site_path(self, path: str) -> str | None:
        """``site-packages/<package path>`` for an archive path inside a source root.

        ``None`` when the path is outside every package root of this
        distribution, so pip never installs it.
        """
        root = next((item for item in self.source_roots if path.startswith(f"{item}/")), None)
        return None if root is None else f"{_SITE_PACKAGES}/{path.removeprefix(f'{root}/')}"

    def installs(self, path: str) -> str | None:
        """The rule that installs ``path`` -- a module, or a ``package-data`` glob -- if any."""
        site = self.site_path(path)
        relative = site.removeprefix(f"{_SITE_PACKAGES}/") if site is not None else ""
        if relative.endswith(".py"):
            return f"{self.root}/pyproject.toml python module"
        for package, patterns in self.package_data:
            prefix = f"{package.replace('.', '/')}/"
            within = PurePosixPath(relative.removeprefix(prefix))
            if relative.startswith(prefix) and any(within.full_match(pattern) for pattern in patterns):
                return f"{self.root}/pyproject.toml package-data {package}: {', '.join(patterns)}"
        return None


def _toml_table(table: Mapping[str, object], key: str) -> Mapping[str, object]:
    """A TOML sub-table, or an empty one when the key is absent or not a table."""
    value = table.get(key)
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _toml_strings(value: object) -> tuple[str, ...]:
    """The strings of a TOML array; anything else contributes nothing."""
    items = cast(list[object], value) if isinstance(value, list) else []
    return tuple(item for item in items if isinstance(item, str))


def _distribution(root: str, members: Mapping[str, bytes]) -> _Distribution | None:
    """Read ``<root>/pyproject.toml`` the way setuptools will: package roots and package data.

    ``None`` when the pyproject is absent; the Formula's own requirement on it
    then names the omission.
    """
    raw = members.get(f"{root}/pyproject.toml")
    if raw is None:
        return None
    try:
        data: dict[str, object] = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"{root}/pyproject.toml is not readable TOML: {exc}") from exc
    setuptools = _toml_table(_toml_table(data, "tool"), "setuptools")
    where = _toml_strings(_toml_table(_toml_table(setuptools, "packages"), "find").get("where"))
    if not where:
        where = ("src",) if any(name.startswith(f"{root}/src/") for name in members) else (".",)
    package_data = tuple((package, _toml_strings(patterns)) for package, patterns in sorted(_toml_table(setuptools, "package-data").items()))
    return _Distribution(root, tuple(posixpath.normpath(posixpath.join(root, item)) for item in where), package_data)


@dataclass(frozen=True)
class PayloadEvidence:
    """Every requirement and finding derived for one staged payload."""

    requirements: tuple[Requirement, ...]
    findings: tuple[Finding, ...]
    modules: tuple[str, ...]

    @property
    def failures(self) -> tuple[Requirement, ...]:
        """Requirements whose leaf is known to be missing or not installed.

        Each one is an artifact-integrity failure: staging refuses on it.
        """
        failed: frozenset[str] = _payload_requirements().FAILED_COVERAGE
        return tuple(item for item in self.requirements if item.coverage in failed)

    @property
    def advisories(self) -> tuple[Finding, ...]:
        """Findings that are never coverage: unresolved anchors and uncovered reads.

        Printed, never refused on: unknown non-integrity data is advisory.
        """
        advisory: frozenset[str] = _payload_requirements().ADVISORY_FINDINGS
        return tuple(item for item in self.findings if item.kind in advisory)

    @property
    def archive_leaves(self) -> tuple[str, ...]:
        """Archive members some reader requires, sorted.

        Leaves outside the payload roots are added to the archive explicitly.
        """
        return tuple(sorted({item.source_leaf for item in self.requirements if item.source_leaf is not None}))

    def bundles(self) -> dict[str, frozenset[str]]:
        """Installed directories the Formula fills from the archive, with their leaf names."""
        bundles: dict[str, set[str]] = {}
        for item in self.requirements:
            directory, name = posixpath.split(item.installed_path)
            if item.source_leaf is not None and directory and not directory.startswith(_SITE_PACKAGES):
                bundles.setdefault(directory, set()).add(name)
        return {directory: frozenset(names) for directory, names in bundles.items()}


class _RequirementMapper:
    """The manager adapter: maps the helper's anchored reads onto the archive.

    A ``sys.prefix`` leaf maps through the Formula statement that produces it;
    a package-relative leaf maps through its distribution's package roots and
    must also match ``package-data``. The Formula's own reads -- each
    ``pip_install`` target's pyproject and each archive file it installs --
    are requirements too, and so are the policy files.
    """

    def __init__(self, layout: _FormulaLayout, members: Mapping[str, bytes], distributions: tuple[_Distribution, ...]) -> None:
        """Map onto one payload: its Formula, its regular members, its distributions."""
        self._helper = _payload_requirements()
        self._layout = layout
        self._members = members
        self._distributions = distributions
        self._requirements: dict[tuple[str, str], Requirement] = {}
        self._findings: list[Finding] = []
        self.runtime_leaves: set[str] = set()

    def requirements(self) -> tuple[Requirement, ...]:
        """Every requirement mapped so far, one per reader and installed path, in stable order."""
        return tuple(self._requirements[key] for key in sorted(self._requirements))

    def findings(self) -> tuple[Finding, ...]:
        """Directory references and bare anchors met while mapping."""
        return tuple(self._findings)

    def read(self, read: AnchoredRead) -> None:
        """Classify one anchored read as a requirement, a directory, or a bare anchor.

        A bare ``sys.prefix`` (an identity comparison, not a resource) and an
        installed directory are findings, never requirements.
        """
        if not read.path:
            self._finding(self._helper.FINDING_ANCHOR_ROOT, read, f"bare {read.anchor} anchor")
        elif read.anchor == self._helper.ANCHOR_PREFIX:
            self._prefix(read)
        else:
            self._source(read)

    def installer(self) -> None:
        """What the Formula itself reads from the archive at install time.

        Each ``pip_install`` target needs its ``pyproject.toml``; each
        ``install Dir[...]`` entry needs its file. ``Dir[]`` silently drops a
        missing literal, which is exactly why each must be checked.
        """
        path = self._layout.path
        for target, line in self._layout.pip_targets:
            self._add(f"{path}:{line}", f"{_SITE_PACKAGES}/{target} distribution", f"{target}/pyproject.toml", f"{path}:{line} pip_install buildpath/{target}")
        for entry in self._layout.entries:
            if entry.kind == _FORMULA_INSTALL and entry.source is not None:
                self._add(f"{path}:{entry.line}", entry.installed_path, entry.source, f"{path}:{entry.line} installs {entry.source}")

    def policy(self, leaves: Mapping[str, str]) -> None:
        """Files shipped by distribution policy rather than read by code (LICENSE, NOTICE)."""
        for leaf, reason in sorted(leaves.items()):
            self._add(f"policy: {reason}", leaf, leaf, "archive root")

    def _add(self, reader: str, installed: str, source: str | None, mapping: str, coverage: str | None = None) -> None:
        """Record one requirement; it stays open (``required``) unless a coverage is known now."""
        requirement: Requirement = self._helper.Requirement(reader, installed, source, mapping, coverage or self._helper.COVERAGE_REQUIRED)
        self._requirements[(reader, installed)] = requirement

    def _finding(self, kind: str, read: AnchoredRead, reason: str) -> None:
        """Record why an anchored read is not a requirement."""
        finding: Finding = self._helper.Finding(kind, read.location, read.expression, reason)
        self._findings.append(finding)

    def _prefix(self, read: AnchoredRead) -> None:
        """A leaf below ``sys.prefix``: produced by a Formula statement, or not at all."""
        layout, installed = self._layout, read.path
        entry = layout.entry(installed)
        if entry is None and layout.holds_below(installed):
            self._finding(self._helper.FINDING_DIRECTORY, read, f"installed directory {installed}")
            return
        self.runtime_leaves.add(posixpath.basename(installed))
        if entry is not None:
            self._formula_entry(read, entry)
            return
        directory, leaf = posixpath.split(installed)
        source_dirs = sorted({posixpath.dirname(source) for source in layout.install_sources(directory)})
        if len(source_dirs) != 1:
            self._add(read.location, installed, None, f"no {layout.path} statement installs it", self._helper.COVERAGE_MISSING)
            return
        mapping = f"{layout.path}:{layout.install_line(directory)} installs {directory} from {source_dirs[0]} but does not list {leaf}"
        self._add(read.location, installed, posixpath.join(source_dirs[0], leaf), mapping, self._helper.COVERAGE_NOT_INSTALLED)

    def _formula_entry(self, read: AnchoredRead, entry: _FormulaEntry) -> None:
        """A leaf the Formula renders, links, or installs from the archive."""
        mapping = f"{self._layout.path}:{entry.line}"
        if entry.kind == _FORMULA_RENDERED:
            self._add(read.location, entry.installed_path, None, f"{mapping} writes it", self._helper.COVERAGE_FORMULA_RENDERED)
        elif entry.kind == _FORMULA_LINKED:
            self._add(read.location, entry.installed_path, None, f"{mapping} links it", self._helper.COVERAGE_FORMULA_LINKED)
        else:
            self._add(read.location, entry.installed_path, entry.source, f"{mapping} installs {entry.source}")

    def _source(self, read: AnchoredRead) -> None:
        """A leaf beside a module: present in the archive, and installed by its distribution."""
        path = read.path
        if path not in self._members and any(name.startswith(f"{path}/") for name in self._members):
            self._finding(self._helper.FINDING_DIRECTORY, read, f"package directory {path}")
            return
        self.runtime_leaves.add(posixpath.basename(path))
        installed, rule = self._installation(path)
        if path not in self._members:
            self._add(read.location, installed, path, "package source", self._helper.COVERAGE_MISSING)
        elif rule is None:
            self._add(read.location, installed, path, "no package-data rule installs it", self._helper.COVERAGE_NOT_INSTALLED)
        else:
            self._add(read.location, installed, path, rule)

    def _installation(self, path: str) -> tuple[str, str | None]:
        """Where a package-relative leaf lands in ``site-packages``, and the rule that puts it there.

        A leaf outside every distribution's package roots keeps its archive
        path and has no rule, so it is reported as not installed.
        """
        for dist in self._distributions:
            installed = dist.site_path(path)
            if installed is not None:
                return installed, dist.installs(path)
        return path, None


def _derive_requirements(members: Mapping[str, bytes], layout: _FormulaLayout, policy: Mapping[str, str]) -> PayloadEvidence:
    """Census the payload's readers, then map every read onto the archive."""
    helper = _payload_requirements()
    distributions = tuple(dist for target, _line in layout.pip_targets if (dist := _distribution(target, members)) is not None)
    census: ReaderCensus = helper.census_readers(members, [root for dist in distributions for root in dist.source_roots])
    mapper = _RequirementMapper(layout, members, distributions)
    for read in census.reads:
        mapper.read(read)
    mapper.installer()
    mapper.policy(policy)
    uncovered: tuple[Finding, ...] = helper.uncovered_reads(census.raw_reads, frozenset(mapper.runtime_leaves))
    findings = sorted((*census.findings, *mapper.findings(), *uncovered), key=lambda item: (item.location, item.kind, item.reason))
    return PayloadEvidence(mapper.requirements(), tuple(findings), census.modules)


def _derive_payload_requirements(checkout: Path, commit: str) -> PayloadEvidence:
    """Derive what the payload needs from the manager commit's own shipped bytes.

    Read from the commit, never the working tree, because the commit is what
    ships. A requirement that already fails here (a missing package resource,
    a Formula-mapped leaf the Formula does not install) refuses before any
    archive exists.
    """
    roots = _paths_at_commit(checkout, commit, _PAYLOAD_SOURCE_PATHS, recursive=False)
    absent = [path for path in _PAYLOAD_SOURCE_PATHS if path not in roots]
    if absent:
        raise SystemExit(f"manager commit {commit} is missing payload roots: {', '.join(absent)}")
    members = _regular_members(_git_archive(checkout, commit, _PAYLOAD_SOURCE_PATHS))
    try:
        evidence = _derive_requirements(members, _FORMULA_LAYOUT, _POLICY_LEAVES)
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise SystemExit(f"a shipped module at {commit} cannot be parsed: {exc}") from exc
    _refuse_failed_requirements(evidence, commit)
    return evidence


def _refuse_failed_requirements(evidence: PayloadEvidence, commit: str) -> None:
    """Artifact-integrity boundary: a known missing leaf is never staged.

    Passes whenever every resolved immutable leaf is a regular archive member
    the installer maps to its installed path -- the state of every complete
    payload; ``stage()`` checks it on each staging, and the complete-payload
    staging smokes exercise the pass path.
    """
    failures = evidence.failures
    if failures:
        raise SystemExit(f"payload at manager commit {commit} is missing required leaves: " + "; ".join(requirement.describe() for requirement in failures))


def _paths_at_commit(checkout: Path, commit: str, paths: tuple[str, ...], *, recursive: bool) -> frozenset[str]:
    """Which of ``paths`` the commit holds.

    Top-level entries by default; every file below them with ``recursive``.
    A pathspec ``git archive`` cannot match would fail the archive outright.
    """
    if not paths:
        return frozenset()
    arguments = ["ls-tree", "-z", "--name-only", *(["-r"] if recursive else []), commit, "--", *paths]
    result = subprocess.run(["git", *arguments], cwd=checkout, check=True, capture_output=True, timeout=_GIT_TIMEOUT_S)
    return frozenset(name.decode("utf-8") for name in result.stdout.split(b"\0") if name)


def _git_archive(checkout: Path, commit: str, paths: tuple[str, ...]) -> bytes:
    """Uncompressed ``git archive`` of ``paths`` at ``commit``: content-addressed, no mtimes."""
    return subprocess.run(
        ["git", "archive", "--format=tar", commit, *paths],
        cwd=checkout,
        check=True,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
    ).stdout


def _regular_members(tar_bytes: bytes) -> dict[str, bytes]:
    """Name to bytes for every REGULAR file member.

    Directories and links are never leaves: a required leaf present only as
    a directory member, or as a link, is missing.
    """
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            extracted = archive.extractfile(member) if member.isfile() else None
            if extracted is not None:
                members[member.name] = extracted.read()
    return members


def _payload_mirrors(evidence: PayloadEvidence, member_names: frozenset[str]) -> tuple[MirrorRecord, ...]:
    """Every archived directory carrying a bundle's leaves, found by directory.

    Evidence, not a gate: a fixture mirror missing a leaf is reported so it
    is never mistaken for installed-layout coverage.
    """
    mirrors: list[MirrorRecord] = []
    for directory, leaves in sorted(evidence.bundles().items()):
        canonical = frozenset(posixpath.dirname(source) for source in _FORMULA_LAYOUT.install_sources(directory))
        mirrors.extend(_payload_requirements().census_mirrors(member_names, leaves, canonical))
    return tuple(mirrors)


def _resolve_identity(checkout: Path, ref: str, label: str) -> _GitIdentity:
    commit = _git(checkout, "rev-list", "-n", "1", ref).strip()
    if not commit:
        raise SystemExit(f"{label} {ref!r} did not resolve to a commit")
    tree_hash = _git(checkout, "rev-parse", f"{commit}^{{tree}}").strip()
    return _GitIdentity(commit=commit, tree_hash=tree_hash)


def _require_manager_checkout_pinned(checkout: Path, identity: _GitIdentity, manager_ref: str) -> None:
    checkout_identity = _resolve_identity(checkout, "HEAD", "manager checkout HEAD")
    if checkout_identity.commit != identity.commit:
        raise SystemExit(f"--manager-checkout must be a Git-Controller-provided worktree pinned at --manager-ref {manager_ref}; HEAD is {checkout_identity.commit}")


def _require_manager_checkout_clean(checkout: Path) -> None:
    checkout_status = _git(checkout, "status", "--porcelain").strip()
    if checkout_status:
        raise SystemExit(f"--manager-checkout must be clean before staging; found changes: {checkout_status}")


def _require_contract_pair(
    *,
    manager_checkout: Path,
    manager_commit: str,
    seed_checkout: Path,
    seed_commit: str,
) -> str:
    """Refuse a release whose manager and locked seed disagree at birth time."""

    manager_digest = _contract_digest_at_commit(
        manager_checkout,
        manager_commit,
        "manager payload",
    )
    seed_digest = _contract_digest_at_commit(
        seed_checkout,
        seed_commit,
        "seed artifact",
    )
    if manager_digest != seed_digest:
        raise SystemExit(f"manager/seed setup contract mismatch; refusing to stage an unbornable release: manager_commit={manager_commit}, manager_digest={manager_digest}, seed_commit={seed_commit}, seed_digest={seed_digest}")
    return manager_digest


def _require_transition_bundle(seed_checkout: Path, seed_commit: str) -> str:
    """Digest the committed transition bundle and refuse a malformed or create-shaped one."""
    files = {Path(relative_path).name: _git_blob(seed_checkout, seed_commit, relative_path, "seed artifact") for relative_path in _TRANSITION_BUNDLE_PATHS}
    try:
        flow: object = json.loads(files["existing_install_flow.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"seed artifact commit {seed_commit} has a malformed existing_install_flow.json: {exc}") from exc
    if not isinstance(flow, dict) or flow.get("flow_id") != "existing-install" or flow.get("schema_version") != 1:
        raise SystemExit("seed artifact existing_install_flow.json does not declare the existing-install flow v1")
    digest = _digest_files(files)
    if digest == _contract_digest_at_commit(seed_checkout, seed_commit, "seed artifact"):
        raise SystemExit("transition bundle digest must differ from the create bundle digest")
    return digest


def _digest_files(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _contract_digest_at_commit(
    checkout: Path,
    commit: str,
    label: str,
) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(_DIGESTED_CONTRACT_PATHS, key=lambda value: Path(value).name):
        name = Path(relative_path).name
        content = _git_blob(checkout, commit, relative_path, label)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _git_blob(checkout: Path, commit: str, relative_path: str, label: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{commit}:{relative_path}"],
        cwd=checkout,
        check=False,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise SystemExit(f"{label} commit {commit} does not provide required setup contract {relative_path}: {detail}")
    return result.stdout


def _output_root(raw: str, *source_trees: Path) -> Path:
    if not raw.strip():
        raise SystemExit("--output-root must be a non-empty path")
    output_root = Path(raw).resolve()
    for source_tree in source_trees:
        try:
            output_root.relative_to(source_tree)
        except ValueError:
            continue
        raise SystemExit(f"--output-root must be outside every source tree; refusing to write into source tree {source_tree}")
    return output_root


def _read_manager_version(checkout: Path) -> str:
    pyproject = checkout / "solet_cli" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"{pyproject} has no [project].version")
    return version


def _build_payload_archive(checkout: Path, commit: str, destination: Path, evidence: PayloadEvidence) -> tuple[str, dict[str, str], PayloadEvidence, tuple[MirrorRecord, ...]]:
    """Deterministic tar.gz of the payload roots plus every derived leaf at
    the resolved commit, a per-file sha256 digest of every member (release
    manifest §7.1 `manager.file_digests`), the evidence verified against the
    archive's regular members, and the bundle mirrors it carries.

    Uses ``git archive`` (content addressed by the commit, no local-clock
    mtimes) piped through gzip with no embedded timestamp, so re-running
    against the same commit reproduces byte-identical output regardless of
    the checkout's own working-tree state. Nothing is written until every
    required leaf is a regular member.
    """
    outside_roots = tuple(leaf for leaf in evidence.archive_leaves if not any(leaf == root or leaf.startswith(f"{root}/") for root in _PAYLOAD_SOURCE_PATHS))
    present = _paths_at_commit(checkout, commit, outside_roots, recursive=True)
    tar_bytes = _git_archive(checkout, commit, (*_PAYLOAD_SOURCE_PATHS, *(leaf for leaf in outside_roots if leaf in present)))
    _refuse_excluded_member(tar_bytes)
    regular = frozenset(_regular_members(tar_bytes))
    requirements: tuple[Requirement, ...] = _payload_requirements().verify_archive(evidence.requirements, regular)
    verified = PayloadEvidence(requirements, evidence.findings, evidence.modules)
    _refuse_failed_requirements(verified, commit)
    file_digests = _payload_file_digests(tar_bytes)
    compressed = _gzip_no_timestamp(tar_bytes)
    destination.write_bytes(compressed)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        member_names = frozenset(archive.getnames())
    return hashlib.sha256(compressed).hexdigest(), file_digests, verified, _payload_mirrors(verified, member_names)


def _payload_file_digests(tar_bytes: bytes) -> dict[str, str]:
    digests: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SystemExit(f"payload archive member {member.name} could not be read for digesting")
            digests[member.name] = f"sha256:{hashlib.sha256(extracted.read()).hexdigest()}"
    return digests


def _build_release_manifest_draft(
    *,
    release_label: str | None,
    manager_release_tag: str | None,
    seed_repository: str,
    seed_release_tag: str,
    seed_identity: _GitIdentity,
    seed_profile: str,
    seed_channel_id: str,
    seed_provenance: dict[str, object],
    manager_source_repository: str,
    manager_identity: _GitIdentity,
    version: str,
    asset_name: str,
    manager_url: str,
    payload_sha256: str,
    file_digests: dict[str, str],
    contract_digest: str,
    transition_digest: str,
    allow_manager_seed_skew: str | None,
) -> dict[str, object]:
    """Stage-5 partial draft of `release_manifest.json` (schema v1, design
    §7.1): the `seed` and `manager` sections this stage can populate. Every
    section a LATER stage of `publish_release` contributes — `components`,
    `bundle_verdict`, `guest_validation`, `tap`, `surface_digests`,
    `produced_by`, `factory_signature` — is null until that stage finalises
    it (§5.4 stage 13); this script never runs those stages."""
    return {
        "schema_version": _RELEASE_MANIFEST_SCHEMA_VERSION,
        "release_label": release_label,
        "manager_release_tag": manager_release_tag,
        "seed": {
            "repository": seed_repository,
            "release_tag": seed_release_tag,
            "commit": seed_identity.commit,
            "tree_hash": seed_identity.tree_hash,
            "seed_id": seed_provenance["seed_id"],
            "manifest_sha256": seed_provenance["manifest_sha256"],
            "source_commit": seed_provenance["source_commit"],
            "profile": seed_profile,
            "channel_id": seed_channel_id,
            "provenance": seed_provenance,
        },
        "components": None,
        "bundle_verdict": None,
        "guest_validation": None,
        "manager": {
            "source_repository": manager_source_repository,
            "source_commit": manager_identity.commit,
            "source_tree_hash": manager_identity.tree_hash,
            "version": version,
            "payload_asset": {
                "name": asset_name,
                "url": manager_url,
                "sha256": f"sha256:{payload_sha256}",
            },
            "file_digests": file_digests,
            "contract_digest": contract_digest,
            "transition_bundle_digest": transition_digest,
            "allow_manager_seed_skew": allow_manager_seed_skew,
        },
        "tap": None,
        "surface_digests": None,
        "produced_by": None,
        "factory_signature": None,
    }


def _refuse_excluded_member(tar_bytes: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        names = archive.getnames()
    if _EXCLUDED_FROM_PAYLOAD in names:
        raise SystemExit(f"{_EXCLUDED_FROM_PAYLOAD} must not be present in the seed checkout's solet_cli/ before archiving — it is rendered separately and ships in the tap, never inside the payload archive")


def _gzip_no_timestamp(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(data)
    return buffer.getvalue()


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
    )
    return result.stdout


def _render(metadata_path: Path, manifest_path: Path, output_root: Path) -> None:
    renderer = Path(__file__).resolve().parent / "render_release_payload.py"
    subprocess.run(
        [
            "python3",
            str(renderer),
            "--metadata",
            str(metadata_path),
            "--manifest",
            str(manifest_path),
            "--output-root",
            str(output_root),
        ],
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
