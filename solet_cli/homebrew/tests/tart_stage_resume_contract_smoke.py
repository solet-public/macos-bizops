#!/usr/bin/env python3
"""No-Tart smoke for the stage-resume host transport boundary."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "solet_cli" / "homebrew" / "ci"))

from tart_stage_resume_contract import (  # noqa: E402
    ADAPTERS,
    ContractError,
    guest_metadata,
    selected_adapters,
    tart_run_argv,
    transport_manifest,
    verify_artifact_digest,
    write_guest_metadata,
    write_transport_manifest,
)

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _reject(callback: object, label: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except ContractError:
        _check(True, label)
    else:
        _check(False, label)


def _origin() -> dict[str, object]:
    return {
        "formula_revision": 0,
        "install_mode": "dev",
        "manager_url": "file:///host/stage/payload/solet-0.1.0.tar.gz",
        "manager_source_repository": "https://github.com/example/solet.git",
        "manager_source_ref": "a" * 40,
        "manager_source_commit": "a" * 40,
        "manager_source_tree_hash": "b" * 40,
        "release_archive_sha256": "c" * 64,
        "seed_repository": "https://github.com/example/seed.git",
        "seed_release_tag": "v0.1.0",
        "seed_commit": "d" * 40,
        "seed_tree_hash": "e" * 40,
        "seed_profile": "fixture",
        "seed_channel_id": "stable",
        "seed_provenance": {
            "schema_version": 1,
            "provenance_sha256": "f" * 64,
            "seed_id": "123e4567-e89b-12d3-a456-426614174000",
            "origin_id": "123e4567-e89b-12d3-a456-426614174001",
            "manifest_sha256": "0" * 64,
            "bundle_name": "fixture",
            "platform": "local",
            "source_commit": "d" * 40,
            "source_date": "2026-09-15T00:00:00+00:00",
        },
        "existing_install_contract": {
            "flow_id": "existing-install",
            "flow_schema_version": 1,
            "bundle_digest": f"sha256:{'1' * 64}",
        },
        "allowed_repository_migrations": [],
    }


def _stable_origin() -> dict[str, object]:
    origin = _origin()
    origin["install_mode"] = "release"
    origin["manager_url"] = (
        "https://github.com/solet-public/homebrew-tap/releases/download/manager-v0.1.0-r71/solet-0.1.0-r71.tar.gz"
    )
    return origin


def _check_stable_origin() -> None:
    """A stable staged origin is a valid transport origin: the manager comes from the
    staged payload that stands in for the not-yet-published release asset."""
    stable = _stable_origin()
    try:
        guest = guest_metadata(stable, "solet-0.1.0-r71.tar.gz")
    except ContractError as exc:
        raise AssertionError(f"a stable origin is a valid transport origin: {exc}") from exc
    _check(
        guest["manager_url"] == "file:///Volumes/solet-stage/origin/payload/solet-0.1.0-r71.tar.gz",
        "stable origin is carried by the staged payload",
    )
    _check(
        guest["install_mode"] == "dev",
        "stable guest metadata pairs the staged file URL with dev mode",
    )
    _check(
        {key for key in stable if stable[key] != guest[key]} == {"manager_url", "install_mode"},
        "stable transport rewrite changes only manager_url and install_mode",
    )
    _check(stable["install_mode"] == "release", "the stable origin metadata is not mutated")
    _check(
        all(guest[key] == stable[key] for key in ("release_archive_sha256", "seed_commit", "seed_tree_hash")),
        "stable transport keeps the payload digest and the seed identity",
    )
    for label, url in (
        ("a file URL", "file:///host/stage/payload/solet-0.1.0-r71.tar.gz"),
        ("a non-GitHub host", "https://example.com/solet-public/homebrew-tap/releases/download/t/solet-0.1.0-r71.tar.gz"),
        ("a plain http URL", "http://github.com/solet-public/homebrew-tap/releases/download/t/solet-0.1.0-r71.tar.gz"),
        ("a non-release GitHub path", "https://github.com/solet-public/homebrew-tap/archive/solet-0.1.0-r71.tar.gz"),
        ("a URL with a query", "https://github.com/solet-public/homebrew-tap/releases/download/t/solet-0.1.0-r71.tar.gz?x=1"),
    ):
        bad = _stable_origin()
        bad["manager_url"] = url
        _reject(lambda bad=bad: guest_metadata(bad, "solet-0.1.0-r71.tar.gz"), f"stable origin with {label} is refused")
    unknown = _stable_origin()
    unknown["install_mode"] = "daily"
    _reject(lambda: guest_metadata(unknown, "solet-0.1.0-r71.tar.gz"), "an unknown install mode is refused")
    _reject(
        lambda: guest_metadata(stable, "solet-0.1.0.tar.gz"),
        "stable asset mismatch is refused before a guest can install",
    )


def main() -> int:
    origin = _origin()
    guest = guest_metadata(origin, "solet-0.1.0.tar.gz")
    _check(selected_adapters("all") == ADAPTERS, "all adapters retain canonical ordering")
    _check(
        all(selected_adapters(adapter) == (adapter,) for adapter in ADAPTERS),
        "each adapter is independently selectable",
    )
    _check(
        guest["manager_url"] == "file:///Volumes/solet-stage/origin/payload/solet-0.1.0.tar.gz",
        "guest metadata names the read-only stage mount",
    )
    _check(
        {key for key in origin if origin[key] != guest[key]} == {"manager_url"},
        "transport rewrite changes only manager_url",
    )
    missing_key = dict(origin)
    del missing_key["seed_channel_id"]
    _reject(
        lambda: guest_metadata(missing_key, "solet-0.1.0.tar.gz"),
        "canonical metadata missing a required key is refused",
    )
    extra_key = dict(origin)
    extra_key["unexpected"] = "metadata"
    _reject(
        lambda: guest_metadata(extra_key, "solet-0.1.0.tar.gz"),
        "canonical metadata with an extra key is refused",
    )
    _reject(lambda: selected_adapters("homebrew,lm_studio"), "combined selector is refused")
    _check_stable_origin()
    _reject(
        lambda: guest_metadata(origin, "other.tar.gz"),
        "asset mismatch is refused before a guest can install",
    )
    with tempfile.TemporaryDirectory() as temporary:
        run_root = Path(temporary)
        (run_root / "stage").mkdir()
        (run_root / "receipts").mkdir()
        metadata_path = run_root / "stage" / "guest" / "release_metadata.json"
        write_guest_metadata(metadata_path, guest)
        _check(metadata_path.is_file(), "guest metadata is staged")
        origin_path = run_root / "stage" / "origin" / "release_metadata.json"
        origin_path.parent.mkdir(parents=True)
        origin_path.write_text(json.dumps(origin), encoding="utf-8")
        payload = run_root / "stage" / "origin" / "payload" / "solet-0.1.0.tar.gz"
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"fixture payload")
        formula = run_root / "stage" / "guest" / "Formula" / "solet.rb"
        formula.parent.mkdir(parents=True)
        formula.write_text(f'url "{guest["manager_url"]}"\n', encoding="utf-8")
        manifest = transport_manifest(
            stage_root=run_root / "stage",
            origin_metadata=origin_path,
            guest_metadata_path=metadata_path,
            payload=payload,
            formula=formula,
            manager_source_commit="a" * 40,
        )
        manifest_path = run_root / "stage" / "transport-manifest.json"
        write_transport_manifest(manifest_path, manifest)
        _check(
            manifest["payload_relative_path"] == "origin/payload/solet-0.1.0.tar.gz",
            "manifest binds the one staged payload without a host path",
        )
        _check(
            manifest["formula_relative_path"] == "guest/Formula/solet.rb",
            "manifest binds the guest-rendered Formula",
        )
        argv = tart_run_argv(run_root, "solet-stage-fixture")
        _check(
            argv[4:6]
            == [
                f"--dir={run_root / 'stage'}:ro,tag=solet-stage",
                f"--dir={run_root / 'receipts'}:tag=solet-receipts",
            ],
            "Tart launch has one read-only stage share and one writable receipt share",
        )
        artifact = run_root / "receipts" / "request.json"
        artifact.write_text("receipt", encoding="utf-8")
        digest = f"sha256:{hashlib.sha256(b'receipt').hexdigest()}"
        verify_artifact_digest(artifact, digest)
        artifact.write_text("drift", encoding="utf-8")
        _reject(
            lambda: verify_artifact_digest(artifact, digest),
            "receipt artifact drift is refused",
        )
    print(f"tart_stage_resume_contract_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
