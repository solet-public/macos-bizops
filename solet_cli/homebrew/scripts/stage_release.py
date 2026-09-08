"""Stage a Homebrew manager payload and an independently identified seed lock.

Builds the deterministic manager-payload archive (the public-distribution
``LICENSE`` and ``NOTICE``, ``solet_cli/``, and the birth-spine setup
contracts), computes its checksum, resolves the seed's own git identity at the
given ref, records the manager source identity, writes ``release_metadata.json``, and then calls the existing
``render_release_payload.py`` to produce the real ``Formula/solet.rb`` and
``solet_cli/homebrew/seed.lock.json``.

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
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_S = 30

_PAYLOAD_SOURCE_PATHS = ("LICENSE", "NOTICE", "solet_setup_contracts", "solet_cli")
_DIGESTED_CONTRACT_PATHS = (
    "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_flow.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_answers.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_journal.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_adapter_envelope.schema.json",
)
_CONTRACT_PATHS = (
    *_DIGESTED_CONTRACT_PATHS,
    "plugins/github_midwife_plugin/knowledge_base/permissions_manifest.json",
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


@dataclass(frozen=True)
class _GitIdentity:
    commit: str
    tree_hash: str


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
    manager_repository = _require_manager_repository(args.manager_repository)
    manager_release_tag = _require_manager_release_tag(args.manager_release_tag)
    manager_ref = _require_manager_ref(args.manager_ref)
    manager_source_repository = _require_manager_source_repository(
        args.manager_source_repository
    )
    _require_paths_present(manager_checkout)
    seed_identity = _resolve_identity(seed_checkout, args.release_tag, "seed release tag")
    manager_identity = _resolve_identity(manager_checkout, manager_ref, "manager ref")
    _require_manager_checkout_pinned(manager_checkout, manager_identity, manager_ref)
    _require_manager_checkout_clean(manager_checkout)
    contract_digest = _require_contract_pair(
        manager_checkout=manager_checkout,
        manager_commit=manager_identity.commit,
        seed_checkout=seed_checkout,
        seed_commit=seed_identity.commit,
    )
    version = _read_manager_version(manager_checkout)
    asset_name = f"solet-{version}.tar.gz"

    payload_dir = output_root / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_path = payload_dir / asset_name
    payload_sha256 = _build_payload_archive(
        manager_checkout, manager_identity.commit, payload_path
    )

    metadata = {
        "formula_revision": args.formula_revision,
        "manager_url": (
            f"https://github.com/{_owner_repo(manager_repository)}/releases/download/"
            f"{manager_release_tag}/{asset_name}"
        ),
        "manager_source_repository": manager_source_repository,
        "manager_source_ref": manager_ref,
        "manager_source_commit": manager_identity.commit,
        "manager_source_tree_hash": manager_identity.tree_hash,
        "release_archive_sha256": payload_sha256,
        "seed_repository": args.seed_repository,
        "seed_release_tag": args.release_tag,
        "seed_commit": seed_identity.commit,
        "seed_tree_hash": seed_identity.tree_hash,
        "seed_profile": args.seed_profile,
    }
    metadata_path = output_root / "release_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    _render(metadata_path, output_root)

    print(f"payload archive:  {payload_path} ({payload_sha256})")
    print(f"release metadata: {metadata_path}")
    print(f"rendered formula: {output_root / 'Formula' / 'solet.rb'}")
    print(f"rendered lock:    {output_root / 'solet_cli' / 'homebrew' / 'seed.lock.json'}")
    print(f"setup contracts:  {contract_digest} (manager payload == seed artifact)")
    print("Nothing was pushed, tagged, released, or uploaded.")
    return 0


def _emit_lock_only(
    checkout: Path, output_root: Path, args: argparse.Namespace
) -> int:
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
        "--lock-only",
        action="store_true",
        help=(
            "emit a bare seed.lock.json only — no payload archive, no Formula. "
            "For an additional seed that ships no Homebrew asset of its own; "
            "the checkout need not contain solet_cli/ or the manager contracts."
        ),
    )
    parser.add_argument(
        "--output-root",
        required=True,
        help="non-empty path outside the seed and manager source trees",
    )
    return parser


def _require_tag(tag: str) -> None:
    if _TAG.fullmatch(tag) is None:
        raise SystemExit(f"--release-tag has an invalid immutable identity shape: {tag!r}")


def _require_seed_repository(repository: str) -> None:
    if _SEED_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit(
            f"--seed-repository must be an explicit GitHub HTTPS .git URL: {repository!r}"
        )


def _require_manager_repository(repository: str | None) -> str:
    if repository is None or _SEED_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit(
            "--manager-repository must be an explicit GitHub HTTPS .git URL"
        )
    return repository


def _require_manager_source_repository(repository: str | None) -> str:
    if repository is None or _SEED_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit(
            "--manager-source-repository must be an explicit GitHub HTTPS .git URL"
        )
    return repository


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


def _owner_repo(seed_repository: str) -> str:
    return seed_repository.removeprefix("https://github.com/").removesuffix(".git")


def _require_paths_present(checkout: Path) -> None:
    missing = [
        path
        for path in (*_PAYLOAD_SOURCE_PATHS, *_CONTRACT_PATHS)
        if not (checkout / path).exists()
    ]
    if missing:
        raise SystemExit(
            "seed checkout is missing required payload paths: " + ", ".join(missing)
        )


def _resolve_identity(checkout: Path, ref: str, label: str) -> _GitIdentity:
    commit = _git(checkout, "rev-list", "-n", "1", ref).strip()
    if not commit:
        raise SystemExit(f"{label} {ref!r} did not resolve to a commit")
    tree_hash = _git(checkout, "rev-parse", f"{commit}^{{tree}}").strip()
    return _GitIdentity(commit=commit, tree_hash=tree_hash)


def _require_manager_checkout_pinned(
    checkout: Path, identity: _GitIdentity, manager_ref: str
) -> None:
    checkout_identity = _resolve_identity(checkout, "HEAD", "manager checkout HEAD")
    if checkout_identity.commit != identity.commit:
        raise SystemExit(
            "--manager-checkout must be a Git-Controller-provided worktree pinned "
            f"at --manager-ref {manager_ref}; HEAD is {checkout_identity.commit}"
        )


def _require_manager_checkout_clean(checkout: Path) -> None:
    checkout_status = _git(checkout, "status", "--porcelain").strip()
    if checkout_status:
        raise SystemExit(
            f"--manager-checkout must be clean before staging; found changes: {checkout_status}"
        )


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
        raise SystemExit(
            "manager/seed setup contract mismatch; refusing to stage an "
            "unbornable release: "
            f"manager_commit={manager_commit}, manager_digest={manager_digest}, "
            f"seed_commit={seed_commit}, seed_digest={seed_digest}"
        )
    return manager_digest


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
        raise SystemExit(
            f"{label} commit {commit} does not provide required setup contract "
            f"{relative_path}: {detail}"
        )
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
        raise SystemExit(
            "--output-root must be outside every source tree; refusing to write into "
            f"source tree {source_tree}"
        )
    return output_root


def _read_manager_version(checkout: Path) -> str:
    pyproject = checkout / "solet_cli" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"{pyproject} has no [project].version")
    return version


def _build_payload_archive(checkout: Path, commit: str, destination: Path) -> str:
    """Deterministic tar.gz of the payload paths at the resolved commit.

    Uses ``git archive`` (content addressed by the commit, no local-clock
    mtimes) piped through gzip with no embedded timestamp, so re-running
    against the same commit reproduces byte-identical output regardless of
    the checkout's own working-tree state.
    """
    tar_bytes = subprocess.run(
        ["git", "archive", "--format=tar", commit, *_PAYLOAD_SOURCE_PATHS, *_CONTRACT_PATHS],
        cwd=checkout,
        check=True,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
    ).stdout
    _refuse_excluded_member(tar_bytes)
    compressed = _gzip_no_timestamp(tar_bytes)
    destination.write_bytes(compressed)
    return hashlib.sha256(compressed).hexdigest()


def _refuse_excluded_member(tar_bytes: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        names = archive.getnames()
    if _EXCLUDED_FROM_PAYLOAD in names:
        raise SystemExit(
            f"{_EXCLUDED_FROM_PAYLOAD} must not be present in the seed checkout's "
            "solet_cli/ before archiving — it is rendered separately and ships "
            "in the tap, never inside the payload archive"
        )


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


def _render(metadata_path: Path, output_root: Path) -> None:
    renderer = Path(__file__).resolve().parent / "render_release_payload.py"
    subprocess.run(
        [
            "python3",
            str(renderer),
            "--metadata",
            str(metadata_path),
            "--output-root",
            str(output_root),
        ],
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
