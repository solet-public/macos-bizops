"""The guest-validation sealed seed bundle seam (iss_3c84d7c3) and the hardening it must not touch.

A release's fresh-guest ladder runs before the public acts, when the lock's tag exists only
in the release host's sealed scratch repo. The ladder carries that tag in as a Git bundle,
and ``materialize_locked_seed(sealed_seed_bundle=...)`` fetches from it. This smoke proves:

* off by default: a create without the bundle fetches ``origin`` exactly as before;
* with the bundle, only the fetch source moves: origin stays the locked repository and the
  tag must still peel to the locked commit (a real Git bundle is fetched through the
  manager's own runner);
* a bundle naming another commit or tag, a tagless lock, or a bad path is refused before
  any Git command runs;
* the three hidden create flags are all-or-none, and the bundle reaches materialize_locked_seed
  through CreateManager, execute_create and _materialize_target (a create without it makes
  exactly the call it always made);
* the hardening is intact: a lock naming a local or file:// origin is refused, url.* stays
  refused config, and the hardened environment still ignores global config.

Run directly: ``.venv/bin/python solet_cli/tests/sealed_seed_bundle_smoke.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import create as create_module  # noqa: E402
from solet_manager import create_execution  # noqa: E402
from solet_manager.cli import build_parser  # noqa: E402
from solet_manager.cli_commands import sealed_seed_bundle  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.create import CreateManager  # noqa: E402
from solet_manager.errors import ConfigError, SourceError, SourceIdentityError  # noqa: E402
from solet_manager.models import CommandResult, ExitCode  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.seed_lock_parser import parse_seed_lock_bytes  # noqa: E402
from solet_manager.source_acquisition import (  # noqa: E402
    SealedSeedBundle,
    materialize_locked_seed,
    subprocess_runner,
)
from solet_manager.target_git import EXECUTABLE_CONFIG, target_git_env  # noqa: E402
from solet_manager.transaction import Transaction  # noqa: E402
from source_acquisition_smoke import FakeGit  # noqa: E402

_REPOSITORY = "https://github.com/solet-public/macos-bizops.git"
_TAG = "release-2026-11-07-944eedfe4ea8"
_CHECKS = [0]


def _check(condition: object, label: str) -> None:
    _CHECKS[0] += 1
    if not condition:
        raise AssertionError(label)


def _seed(*, tag: str | None = _TAG) -> SeedLock:
    return SeedLock(_REPOSITORY, tag, "a" * 40, "b" * 40, "c" * 64, "macos-bizops")


def _fetches(fake: FakeGit) -> list[tuple[str, ...]]:
    return [command for command in fake.commands if command[:2] == ("git", "fetch")]


def _check_default_fetches_origin(root: Path) -> None:
    seed = _seed()
    fake = FakeGit(seed)
    materialize_locked_seed(seed, root / "default" / "instance", cache_dir=root / "cache", runner=fake)
    _check(
        _fetches(fake) == [("git", "fetch", "--no-tags", "origin", f"refs/tags/{_TAG}:refs/tags/{_TAG}")],
        "a create without the bundle fetches the locked tag from origin, unchanged",
    )


def _check_bundle_moves_only_the_fetch_source(root: Path) -> None:
    seed = _seed()
    bundle = root / "sealed-seed.bundle"
    bundle.write_bytes(b"bundle bytes are faked here; the real fetch is checked below")
    fake = FakeGit(seed)
    target = root / "bundled" / "instance"
    materialize_locked_seed(
        seed,
        target,
        cache_dir=root / "cache",
        runner=fake,
        sealed_seed_bundle=SealedSeedBundle(bundle, seed.commit, _TAG),
    )
    _check(target.is_dir(), "the bundled create materializes the target")
    _check(
        _fetches(fake) == [("git", "fetch", "--no-tags", str(bundle), f"refs/tags/{_TAG}:refs/tags/{_TAG}")],
        "the bundled create fetches the locked tag from the bundle, never origin",
    )
    _check(("git", "remote", "add", "origin", _REPOSITORY) in fake.commands, "origin stays the locked repository")
    _check(("git", "remote", "get-url", "origin") in fake.commands, "the origin==lock pin still runs")
    _check(("git", "rev-parse", f"refs/tags/{_TAG}^{{commit}}") in fake.commands, "the fetched tag is still peeled")

    drifted = FakeGit(seed, commit="e" * 40)
    try:
        materialize_locked_seed(
            seed,
            root / "drifted" / "instance",
            cache_dir=root / "cache",
            runner=drifted,
            sealed_seed_bundle=SealedSeedBundle(bundle, seed.commit, _TAG),
        )
    except SourceIdentityError as exc:
        _check("peeled to" in str(exc), "a bundled tag peeling elsewhere is refused by the existing check")
    else:
        raise AssertionError("a bundled tag peeling to another commit was accepted")


def _expect_refusal(
    root: Path,
    name: str,
    seed: SeedLock,
    bundle: SealedSeedBundle,
    error: type[Exception],
) -> None:
    fake = FakeGit(seed)
    target = root / name / "instance"
    try:
        materialize_locked_seed(seed, target, cache_dir=root / "cache", runner=fake, sealed_seed_bundle=bundle)
    except error:
        _check(fake.commands == [], f"{name}: refused before any Git command")
        _check(not target.parent.exists(), f"{name}: refused before any staging directory")
        return
    raise AssertionError(f"{name}: accepted")


def _check_bundle_refusals(root: Path) -> None:
    bundle = root / "refusal.bundle"
    bundle.write_bytes(b"x")
    seed = _seed()
    _expect_refusal(root, "other-commit", seed, SealedSeedBundle(bundle, "d" * 40, _TAG), SourceIdentityError)
    _expect_refusal(root, "other-tag", seed, SealedSeedBundle(bundle, seed.commit, "release-2026-11-06-944eedfe4ea8"), SourceIdentityError)
    _expect_refusal(root, "tagless-lock", _seed(tag=None), SealedSeedBundle(bundle, seed.commit, _TAG), SourceIdentityError)
    _expect_refusal(root, "relative-path", seed, SealedSeedBundle(Path("refusal.bundle"), seed.commit, _TAG), SourceError)
    _expect_refusal(root, "missing-file", seed, SealedSeedBundle(root / "absent.bundle", seed.commit, _TAG), SourceError)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _check_real_bundle_fetch(root: Path) -> None:
    """A real bundle of one lightweight tag, fetched through the manager's own hardened runner."""
    sealed = root / "sealed"
    sealed.mkdir()
    _git("init", "--initial-branch=main", cwd=sealed)
    (sealed / "README.md").write_text("sealed\n", encoding="utf-8")
    _git("add", "README.md", cwd=sealed)
    _git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "sealed", cwd=sealed)
    commit = _git("rev-parse", "HEAD", cwd=sealed)
    _git("tag", _TAG, commit, cwd=sealed)
    bundle = root / "real.bundle"
    _git("bundle", "create", str(bundle), f"refs/tags/{_TAG}", cwd=sealed)
    staging = root / "guest-staging"
    _git("init", "--initial-branch=main", str(staging), cwd=root)
    tag_ref = f"refs/tags/{_TAG}"
    fetched = subprocess_runner(("git", "fetch", "--no-tags", str(bundle), f"{tag_ref}:{tag_ref}"), staging, 60)
    _check(fetched.returncode == 0, f"the manager runner fetches the tag from a bundle: {fetched.stderr}")
    _check(_git("rev-parse", f"{tag_ref}^{{commit}}", cwd=staging) == commit, "the bundled tag peels to the sealed commit")


def _parse(*extra: str) -> argparse.Namespace:
    return build_parser().parse_args(["create", "instance", *extra])


def _check_flags_all_or_none() -> None:
    _check(sealed_seed_bundle(_parse()) is None, "no flags: no bundle (the default)")
    full = sealed_seed_bundle(
        _parse(
            "--guest-validation-sealed-seed-bundle",
            "/Volumes/solet-stage/origin/seed/sealed-seed.bundle",
            "--guest-validation-sealed-seed-commit",
            "a" * 40,
            "--guest-validation-sealed-seed-tag",
            _TAG,
        )
    )
    _check(
        full == SealedSeedBundle(Path("/Volumes/solet-stage/origin/seed/sealed-seed.bundle"), "a" * 40, _TAG),
        "all three flags: the bundle carries the path, commit and tag",
    )
    partials: tuple[tuple[str, ...], ...] = (
        ("--guest-validation-sealed-seed-bundle", "/b"),
        ("--guest-validation-sealed-seed-commit", "a" * 40, "--guest-validation-sealed-seed-tag", _TAG),
    )
    for partial in partials:
        try:
            sealed_seed_bundle(_parse(*partial))
        except ConfigError:
            _check(True, "a partial flag set is refused")
        else:
            raise AssertionError(f"partial flag set accepted: {partial}")
    help_text = io.StringIO()
    with contextlib.redirect_stdout(help_text):
        try:
            build_parser().parse_args(["create", "--help"])
        except SystemExit:
            pass
    _check("--target" in help_text.getvalue(), "control: create --help was captured")
    _check("guest-validation" not in help_text.getvalue(), "the flags are hidden from create --help")


class _StopProbeError(Exception):
    """Ends a plumbing probe once the recorded call has happened."""


def _stub_result(*_args: object, **_kwargs: object) -> CommandResult:
    return CommandResult(kind="create", status="awaiting_user", message="stub", exit_code=ExitCode.HUMAN_ACTION)


def _check_create_plumbing(root: Path) -> None:
    bundle = SealedSeedBundle(Path("/Volumes/solet-stage/origin/seed/sealed-seed.bundle"), "a" * 40, _TAG)
    paths = ManagerPaths.resolve(explicit_home=root / "manager", home=root)
    config = CreateConfig(name="plumbing", target=root / "Solets" / "plumbing", autostart=False)
    seen: list[dict[str, object]] = []

    def record(**kwargs: object) -> CommandResult:
        seen.append(kwargs)
        return _stub_result()

    with patch.object(create_module, "execute_create", record):
        CreateManager(paths=paths, contract_directory=None, seed_lock_path=root / "seed.lock.json", sealed_seed_bundle=bundle).create(config, approved_fingerprint="sha256:" + "f" * 64)
    _check(seen[-1].get("sealed_seed_bundle") is bundle, "CreateManager.create hands the bundle to execute_create")

    def ready(*_args: object, **_kwargs: object) -> CommandResult:
        return CommandResult(kind="create_preview", status="preview_ready", message="ready", exit_code=ExitCode.OK, data={"approval_fingerprint": "sha256:" + "f" * 64})

    with patch.object(create_execution, "_execute_locked_create", record):
        create_execution.execute_create(
            paths=paths,
            contract_directory=None,
            seed_lock_path=root / "seed.lock.json",
            registry=InstanceRegistry(paths.registry_path),
            preview_call=ready,
            config=config,
            approved_fingerprint="sha256:" + "f" * 64,
            sealed_seed_bundle=bundle,
        )
    _check(seen[-1].get("sealed_seed_bundle") is bundle, "execute_create hands the bundle to the locked create")
    calls: list[dict[str, object]] = []

    def materialize(_seed: object, _target: Path, **kwargs: object) -> Path:
        calls.append(kwargs)
        raise _StopProbeError

    transaction = cast(Transaction, SimpleNamespace(seed=_seed()))
    materialize_target = create_execution._materialize_target  # pyright: ignore[reportPrivateUsage]
    with patch.object(create_execution, "materialize_locked_seed", materialize):
        for carried in (None, bundle):
            with contextlib.suppress(_StopProbeError):
                materialize_target(paths, config, transaction, carried)
    _check(calls[0] == {"cache_dir": paths.acquisition_dir}, f"a create without the bundle makes exactly the old call: {calls[0]}")
    _check(calls[1] == {"cache_dir": paths.acquisition_dir, "sealed_seed_bundle": bundle}, f"the guest-validation create passes the bundle: {calls[1]}")


def _lock(repository: str) -> bytes:
    return json.dumps({"schema_version": 1, "repository": repository, "release_tag": _TAG, "commit": "a" * 40, "tree_hash": "b" * 40, "profile": "macos-bizops"}).encode()


def _refused_config(key: str) -> bool:
    return any(pattern.fullmatch(key) for pattern in EXECUTABLE_CONFIG)


def _check_hardening_intact() -> None:
    _check(parse_seed_lock_bytes(_lock(_REPOSITORY)).repository == _REPOSITORY, "control: a GitHub lock parses")
    for local in ("file:///Volumes/solet-stage/origin/seed/sealed-seed.bundle", "/Volumes/solet-stage/origin/seed"):
        try:
            parse_seed_lock_bytes(_lock(local))
        except SourceError as exc:
            _check("GitHub HTTPS" in str(exc), "a normal create refuses a lock naming a local origin")
        else:
            raise AssertionError(f"a lock naming a local origin was accepted: {local}")
    _check(_refused_config("url.file:///volumes/solet-stage/.insteadof"), "a url.* rewrite stays refused target config")
    environment = target_git_env()
    _check(environment["GIT_CONFIG_GLOBAL"] == "/dev/null" and environment["GIT_CONFIG_NOSYSTEM"] == "1", "global and system config stay ignored")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _check_default_fetches_origin(root)
        _check_bundle_moves_only_the_fetch_source(root)
        _check_bundle_refusals(root)
        _check_real_bundle_fetch(root)
        _check_create_plumbing(root)
    _check_flags_all_or_none()
    _check_hardening_intact()
    print(f"sealed_seed_bundle_smoke: {_CHECKS[0]} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
