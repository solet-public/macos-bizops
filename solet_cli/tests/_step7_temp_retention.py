"""Step7 cache publication and serialization, inside the folder of the run that uses it.

The cache is made inside the run's folder and ends with that run. In a smoke
battery that folder is the battery's registered temp folder, which the runner
removes at close. In a smoke run by hand the process makes one ``step7-run-``
folder in the system temp folder and removes it at exit. Nothing is kept
between runs, nothing is written under ``~/.ananta`` and nothing is registered.
"""

from __future__ import annotations

import atexit
import ctypes
import errno
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from _step5_support import CANONICAL, FakeHost, Release, git
from _step6_support import byte_map

if TYPE_CHECKING:
    from _step7_support import Knobs, RealStyleFixture
    from solet_manager.models import JsonValue

#: ``_step7_support`` re-exports the private builders below; naming them here records that they are its surface.
__all__ = [
    "PROFILE",
    "_assert_attribution",
    "_bare_clone_shape",
    "_builder_digest",
    "_cache_root",
    "_cached",
    "_clone",
    "_head_commit",
    "_release_from_json",
    "_release_json",
    "_seed_key",
    "checks_by_id",
    "clone_tree",
    "fixture_bytes",
    "make_offline",
    "porcelain",
    "tree_snapshot",
]

_ROOT = Path(__file__).resolve().parents[2]
PROFILE = "macos-bizops"
_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def _head_commit() -> str:
    return git(_ROOT, "rev-parse", "HEAD")


def _builder_digest() -> str:
    """Hash support code and the loaded factory package's Python sources on every call."""
    digest = hashlib.sha256()
    for module in ("_step5_support.py", "_step7_support.py", "_step7_temp_retention.py"):
        digest.update((Path(__file__).resolve().parent / module).read_bytes())
    assembly = importlib.import_module("seed_factory_plugin.assemble")
    package = Path(cast(str, assembly.__file__)).resolve().parent
    for source in sorted(package.rglob("*.py"), key=lambda entry: entry.relative_to(package).as_posix()):
        name = source.relative_to(package).as_posix().encode("utf-8")
        content = source.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:8]


_RUN: dict[str, Path] = {}


def _run_folder() -> Path:
    """The folder of this run: the battery's temp folder, else one ``step7-run-`` folder the process removes at exit."""
    temporary = Path(tempfile.gettempdir()).resolve()
    if (temporary / ".owner.json").is_file():
        return temporary
    if "path" not in _RUN:
        _RUN["path"] = Path(tempfile.mkdtemp(prefix="step7-run-", dir=temporary))
        atexit.register(_end_process_run, _RUN["path"])
    return _RUN["path"]


def _end_process_run(path: Path) -> None:
    """Remove this process's run folder at exit; a failure is reported and the folder is left to the system's temp cleaning."""
    try:
        shutil.rmtree(path)
    except OSError as error:
        print(f"step7 cache advisory: run folder {path} retained ({error})", file=sys.stderr)


def _cache_root() -> Path:
    """The cache folder for this commit and builder, inside the run's folder, which ends it."""
    root = _run_folder() / f"step7-cache-{_head_commit()[:12]}-{_builder_digest()}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cached(name: str, build: Callable[[Path], None]) -> Path:
    """``<cache>/<name>``, built into a private sibling and renamed into place; a concurrent winner is reused."""
    root = _cache_root()
    final = root / name
    if (final / ".complete").is_file():
        return final
    staging = Path(tempfile.mkdtemp(prefix=f"{name}.build-", dir=root))
    build(staging)
    (staging / ".complete").write_text("complete\n", encoding="utf-8")
    try:
        os.rename(staging, final)
    except OSError:
        shutil.rmtree(staging, True)
        if not (final / ".complete").is_file():
            raise
    return final


def _seed_key(knobs: Knobs) -> tuple[object, ...]:
    return (knobs.git_metadata == "gitattributes_tracked_edit", knobs.router, knobs.shipped_strategy, knobs.knowledge_removal, knobs.overlap, knobs.untracked_collision, knobs.kb_addition is not None, knobs.candidate_files)


def _release_json(release: Release) -> dict[str, str]:
    return {"commit": release.commit, "tree": release.tree, "provenance": release.provenance.decode("utf-8"), "seed_id": release.seed_id, "manifest": release.manifest, "source_commit": release.source_commit, "tag": release.tag}


def _release_from_json(value: dict[str, str]) -> Release:
    return Release(value["commit"], value["tree"], value["provenance"].encode("utf-8"), value["seed_id"], value["manifest"], value["source_commit"], value["tag"])


def _clone(source: Path, target: Path, baseline: Release, canonical: str = CANONICAL) -> None:
    completed = subprocess.run(("git", "clone", "--quiet", "--no-hardlinks", str(source), str(target)), check=False, capture_output=True, text=True, env=_ENV)
    if completed.returncode != 0:
        raise AssertionError(f"fixture clone of {source} failed: {completed.stderr.strip()}")
    git(target, "checkout", "--quiet", "-B", "main", baseline.commit)
    git(target, "remote", "set-url", "origin", canonical)


_CLONE_NOFOLLOW = 0x0001


def _clonefile(source: Path, target: Path) -> None:
    """One ``clonefile(2)`` call: APFS clones the whole directory hierarchy copy-on-write, without following links."""
    if sys.platform != "darwin":
        raise OSError(errno.ENOTSUP, f"clonefile(2) is macOS-only, not {sys.platform}")
    function = ctypes.CDLL(None, use_errno=True).clonefile
    function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    function.restype = ctypes.c_int
    if cast(int, function(os.fsencode(source), os.fsencode(target), _CLONE_NOFOLLOW)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(source), None, str(target))


def clone_tree(source: Path, target: Path) -> None:
    """Snapshot ``source`` at the absent ``target``: the same bytes, modes, symlinks and mtimes as
    ``shutil.copytree(source, target, symlinks=True)``, at the cost of one syscall instead of a full copy.

    The APFS clone shares data blocks until either side writes, and a write to either side lands in fresh
    blocks of that side only, so a later edit to ``source`` cannot change ``target`` (and the reverse). Any
    clone failure (another filesystem, another volume, another platform) falls back to that full copy and
    says so in one stderr advisory; it never refuses (rul_becb4bed)."""
    try:
        _clonefile(source, target)
    except (OSError, AttributeError) as exc:
        print(f"step7 clone advisory: copy-on-write clone of {source.name} unavailable ({exc}); full copy used", file=sys.stderr)
        shutil.copytree(source, target, symlinks=True)


def porcelain(target: Path, *, ignored: bool = False) -> dict[str, str]:
    """``git status --porcelain=v1 --untracked-files=all`` as ``{path: XY}`` (the leading status space is significant)."""
    args = ["status", "--porcelain=v1", "--untracked-files=all", *(["--ignored"] if ignored else [])]
    completed = subprocess.run(("git", "-C", str(target), *args), check=True, capture_output=True, text=True, env=_ENV)
    return {line[3:]: line[:2] for line in completed.stdout.splitlines() if len(line) > 3}


def _assert_attribution(target: Path, expected: dict[str, str]) -> None:
    """Section 3.3's completeness proof, both directions, on every build (criterion 16)."""
    observed = porcelain(target)
    ignored = {path for path, code in porcelain(target, ignored=True).items() if code == "!!"}
    unattributed = sorted(set(observed) - set(expected))
    unseen = sorted(path for path in expected if path not in observed and path not in ignored)
    assert not unattributed, f"porcelain entries no writer accounts for: {unattributed}"
    assert not unseen, f"writer outputs neither observed nor ignored: {unseen}"


def make_offline(fake: FakeHost) -> None:
    """``service="offline"``: the bridge does not answer (every bridge read raises) and the process table is unreadable,
    while launchd still lists the label -- the "unreachable" shape whose doctor reading is ``unknown service_offline``
    on sections 9/11/14.  A label launchd no longer lists is the ``launchagent_not_loaded`` finding, not an unknown."""
    from solet_manager.errors import AdapterError  # noqa: PLC0415

    def unreachable(*_args: object, **_kwargs: object) -> dict[str, JsonValue]:
        raise AdapterError("bridge call did not succeed: connection refused")

    fake.read_health = unreachable  # type: ignore[method-assign]
    fake.invoke_bridge = unreachable  # type: ignore[method-assign]
    fake.ps_fails = True


def _bare_clone_shape(target: Path) -> None:
    """``genesis_untracked=False``: the only tree a v4 journal can have been written on (CH-43) -- a clean clone with the ignored runtime only."""
    (target / "profile" / "config").mkdir(parents=True, exist_ok=True)
    (target / "profile" / "config" / "manifest.yaml").write_text(f"profile_name: {PROFILE}\nplugins:\n- github_midwife_plugin\n", encoding="utf-8")
    exclude = target / ".git" / "info" / "exclude"
    exclude.write_text(".venv/\nprofile/\n", encoding="utf-8")


def tree_snapshot(root: Path, *, exclude: tuple[str, ...] = ("profile/data",)) -> dict[str, str]:
    """sha256 (files) / target (symlinks) of everything under ``root`` outside ``.git`` and ``exclude`` prefixes."""
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if ".git" in path.parts or any(relative.startswith(prefix) for prefix in exclude):
            continue
        if path.is_symlink():
            result[relative] = "link:" + os.readlink(path)
        elif path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def fixture_bytes(fixture: RealStyleFixture) -> dict[str, str]:
    """Step 6's ``byte_map`` (target + HOME) plus every symlink by its target, for the sweep and the shared verdict rows.

    ``.solet/genesis.json`` is hashed without its ``completed_at`` stamp (genesis writes the wall clock) so two
    fixtures of one scenario compare equal.
    """
    combined = byte_map(fixture)
    for path in sorted(fixture.target.rglob("*")):
        if path.is_symlink() and ".git" not in path.parts:
            combined[f"target/{path.relative_to(fixture.target)}"] = "link:" + os.readlink(path)
    marker = fixture.target / ".solet" / "genesis.json"
    if marker.is_file():
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload.pop("completed_at", None)
        combined["target/.solet/genesis.json"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return combined


def checks_by_id(doctor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The doctor's sections flattened to ``{check_id: check}``."""
    found: dict[str, dict[str, Any]] = {}
    for section in cast(list[dict[str, Any]], doctor.get("sections", [])):
        for check in cast(list[dict[str, Any]], section["checks"]):
            found[cast(str, check["check_id"])] = check
    return found
