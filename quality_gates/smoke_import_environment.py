"""Own one smoke interpreter, without changing installed dependencies or sources.

Trusted descendants that retain sys.executable retain these mappings after
scrubbing PYTHONPATH. Deliberately replacing the interpreter is outside this
contract. Unknown provisioning/origins advise; measured wrong owned origins
are evidence-integrity failures. Receipts describe this run only.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import TracebackType
from typing import cast

from quality_gates import candidate_repo
from quality_gates.editable_finder import rehome_finder

_FINDER_NAME = "__editable___runner_owned_finder"
_FINDER = """import os, sys
from importlib.machinery import ModuleSpec, PathFinder
from importlib.util import spec_from_file_location
from pathlib import Path
MAPPING = MAPPING_VALUE
class Finder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        value = MAPPING.get(fullname)
        if value is None:
            return None
        if path is None:
            roots = [root for root in os.environ.get('PYTHONPATH', '').split(os.pathsep) if root]
            spec = PathFinder.find_spec(fullname, roots)
            if spec is not None:
                return spec
        root = Path(value)
        if root.is_file():
            return spec_from_file_location(fullname, root)
        init = root / '__init__.py'
        if init.is_file():
            return spec_from_file_location(fullname, init)
        spec = ModuleSpec(fullname, None, is_package=True)
        spec.submodule_search_locations = [str(root)]
        return spec
def install():
    sys.meta_path.insert(0, Finder)
"""
_PROBE = """import json, sys
def locate(name):
    search = None
    spec = None
    parts = name.split('.')
    for index in range(len(parts)):
        fullname = '.'.join(parts[:index + 1])
        spec = next((s for finder in sys.meta_path
                     if (s := finder.find_spec(fullname, search)) is not None), None)
        if spec is None:
            return None
        search = spec.submodule_search_locations
    return spec.origin if spec.origin and spec.origin != 'namespace' else next(iter(search or []), None)
origins = {}
for name in json.loads(sys.argv[1]):
    try:
        origins[name] = locate(name)
    except Exception:
        origins[name] = None
print(json.dumps(dict(prefix=sys.prefix, executable=sys.executable, origins=origins)))
"""


class ImportOriginError(RuntimeError):
    """A known owned origin or private owner identity contradicts this run."""


def _source_roots(root: Path) -> list[Path]:
    return [root / "ananta/src", *sorted((root / "plugins").glob("*/src")), root / "solet_setup_contracts/src", root / "solet_cli/src"]


def _declared_packages(project: Path) -> dict[str, Path]:
    """Read literal setuptools package-dir declarations, including non-src dirs."""
    config = project / "pyproject.toml"
    if not config.is_file():
        return {}
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    setuptools = data.get("tool", {}).get("setuptools", {})
    directories: dict[str, str] = setuptools.get("package-dir", {})
    packages: object = setuptools.get("packages", [])
    result = {name: project / relative for name, relative in directories.items() if name}
    if isinstance(packages, list):
        for name in cast(list[object], packages):
            if isinstance(name, str):
                result.setdefault(name, project / directories.get("", "src") / name.replace(".", "/"))
    return result


def _root_packages(source: Path) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for entry in sorted(source.iterdir()):
        name = entry.stem if entry.suffix == ".py" else entry.name
        if name.isidentifier() and (entry.is_dir() or entry.suffix == ".py") and name != "__pycache__":
            mapping[name] = entry
    return mapping


def owned_packages(root: Path) -> dict[str, Path]:
    """Census package paths from this runner tree, never from installed wheels."""
    mapping: dict[str, Path] = {}
    for source in _source_roots(root):
        if not source.is_dir():
            continue
        mapping.update(_root_packages(source))
        mapping.update(_declared_packages(source.parent))
    router = root / "plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/router.py"
    if router.is_file():
        mapping["macos_self_deployment_plugin.blue_green_router.router"] = router
    for name, path in mapping.items():
        if not path.resolve().is_relative_to(root):
            raise ImportOriginError(f"OWNED_PACKAGE_PATH_ESCAPES_RUNNER_TREE: {name}: {path} -> {path.resolve()}")
    return mapping


def _write_private_text(path: Path, text: str) -> None:
    """Remove a copied link before writing; shared package bytes stay untouched."""
    path.unlink(missing_ok=True)
    path.write_text(text, encoding="utf-8")


def _install_owned_packages(site: Path, source: Path, target: Path, mapping: dict[str, Path]) -> None:
    original = {name: str(source / path.relative_to(target)) for name, path in mapping.items()}
    payload = rehome_finder(_FINDER.replace("MAPPING_VALUE", repr(original)), source, target, _FINDER_NAME)
    _write_private_text(site / f"{_FINDER_NAME}.py", payload)
    _write_private_text(site / "__editable__.000_runner_owned.pth", f"import {_FINDER_NAME}; {_FINDER_NAME}.install()\n")


def probe_origins(python: Path, root: Path, mapping: dict[str, Path]) -> dict[str, object]:
    """Ask the selected interpreter's real finders, without executing packages."""
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    result = subprocess.run([str(python), "-c", _PROBE, json.dumps(sorted(mapping))], cwd=root, env=environment, check=False, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"origin probe exited {result.returncode}: {result.stderr.strip()[-300:]}")
    view: object = json.loads(result.stdout)
    if not isinstance(view, dict):
        raise ValueError("origin probe did not return an object")
    return cast(dict[str, object], view)


def _origin_rows(mapping: dict[str, Path], view: dict[str, object]) -> tuple[list[dict[str, object]], int]:
    raw = view.get("origins")
    origins = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    rows: list[dict[str, object]] = []
    unknown = 0
    for name, expected in sorted(mapping.items()):
        observed = origins.get(name)
        if not isinstance(observed, str) or not expected.exists():
            unknown += 1
            continue
        actual = Path(observed).resolve()
        wanted = expected.resolve()
        matches = actual == wanted or (expected.is_dir() and actual == wanted / "__init__.py")
        if not matches:
            raise ImportOriginError(f"OWNED_IMPORT_ORIGIN_IS_NOT_RUNNER_TREE: {name}: expected={wanted}; observed={actual}")
        rows.append({"module": name, "expected": str(wanted), "observed": str(actual)})
    return rows, unknown


class SmokeImportEnvironment:
    """Create/read/correct/clear only an environment owned by this battery.

    Target changes rebuild this object's owned leaf. Another object's leaf or
    a replaced/symlinked owner is an integrity error. On suite errors the runner
    aborts supervised children before this context removes its environment.
    BatteryRuntime also removes its outer scratch after all children terminate.
    """

    def __init__(self, root: Path, python: Path, scratch: Path | None) -> None:
        self.root = root.resolve()
        self.original_python = python
        self.python = python
        self.scratch = scratch.resolve() if scratch else None
        self.path = self.scratch / "import-environment" if self.scratch else None
        self._owner = secrets.token_hex(16)
        self._created = False
        self._prepared_identity: tuple[Path, tuple[tuple[str, Path], ...]] | None = None
        self.receipt: dict[str, object] = {}

    def _advise(self, reason: object) -> None:
        self.receipt["state"] = "advisory"
        if "advisory" in self.receipt:
            return
        self.receipt["advisory"] = str(reason)[:500]
        print(f"ADVISORY run_smokes UNKNOWN_ORIGIN_ADVISES_AND_RUNS: {reason}", file=sys.stderr)

    def _assert_owner(self) -> None:
        if self.path is None or self.path.is_symlink() or self.path.parent != self.scratch:
            raise ImportOriginError(f"PRIVATE_ENVIRONMENT_OWNER_MISMATCH: {self.path}")
        try:
            owner = (self.path / ".smoke-owner").read_text(encoding="utf-8")
        except OSError as exc:
            raise ImportOriginError(f"PRIVATE_ENVIRONMENT_OWNER_MISMATCH: {self.path}: {exc}") from exc
        if not self._created or owner != self._owner:
            raise ImportOriginError(f"PRIVATE_ENVIRONMENT_OWNER_MISMATCH: {self.path}")

    def _provision(self, mapping: dict[str, Path]) -> None:
        if self.path is None:
            raise OSError("battery scratch unavailable")
        identity = (self.root, tuple(sorted(mapping.items())))
        if self.path.exists() or self.path.is_symlink():
            self._assert_owner()
            if self._prepared_identity == identity:
                return
            self.close()
        self.path.mkdir()
        self._created = True
        (self.path / ".smoke-owner").write_text(self._owner, encoding="utf-8")
        shared_venv = self.original_python.parent.parent
        source = candidate_repo._venv_source_root(shared_venv, self.root)
        candidate_repo.provision_private_venv(self.root, self.root, self.path, shared_venv=shared_venv, require_editable=False)
        _install_owned_packages(candidate_repo._site_packages(self.path), source, self.root, mapping)
        self._prepared_identity = identity
        self.python = self.path / "bin/python3"

    def prepare(self) -> SmokeImportEnvironment:
        self.root = self.root.resolve()
        self.receipt = {"runner_root": str(self.root), "state": "unmeasured", "private": False}
        mapping: dict[str, Path] = {}
        try:
            mapping = owned_packages(self.root)
            if not mapping:
                raise OSError("runner package roots unavailable")
            self._provision(mapping)
            self.receipt["private"] = True
        except (OSError, ValueError, TypeError, AttributeError, candidate_repo.CandidateRepoError) as exc:
            self.python = self.original_python
            self._advise(exc)
        self.receipt["interpreter"] = str(self.python)
        if self.receipt["private"]:
            self._verify(mapping)
        else:
            # Preparation failed: retain the existing direct-child route and
            # disclose that interpreter-owned origins could not be verified.
            self.receipt.update(origins=[], measured=0, unknown=len(mapping), verification="not_provisioned")
        print("import_environment: " + json.dumps(self.receipt, sort_keys=True), file=sys.stderr)
        return self

    def _verify(self, mapping: dict[str, Path]) -> None:
        try:
            view = probe_origins(self.python, self.root, mapping)
            rows, unknown = _origin_rows(mapping, view)
            # Keep declared resource and daemon origins visible even when the
            # tree has more than the receipt's 32 retained module rows.
            rows.sort(key=lambda row: ("." not in str(row["module"]), str(row["module"])))
            self.receipt.update(prefix=view.get("prefix"), origins=rows[:32], measured=len(rows), omitted=max(0, len(rows) - 32), unknown=unknown)
            prefix = view.get("prefix")
            if not isinstance(prefix, str):
                self._advise("interpreter prefix unknown")
            elif self.receipt["private"] and Path(prefix).resolve() != self.path:
                raise ImportOriginError(f"PRIVATE_INTERPRETER_PREFIX: expected={self.path}; observed={view.get('prefix')}")
            if unknown or not mapping:
                self._advise(f"{unknown} unknown origins; {len(mapping)} owned package paths")
            elif self.receipt["private"] and self.receipt["state"] != "advisory":
                self.receipt["state"] = "verified"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self._advise(exc)
        except ImportOriginError as exc:
            self.receipt.update(state="integrity_failure", integrity_failure=str(exc))
            raise

    def close(self) -> None:
        if self._created:
            self._assert_owner()
            assert self.path is not None
            try:
                shutil.rmtree(self.path)
            except OSError as exc:
                self._advise(f"owned environment retained: {exc}")
                return
            self._created = False
        self.python = self.original_python

    def __enter__(self) -> SmokeImportEnvironment:
        try:
            return self.prepare()
        except BaseException:
            self.close()
            raise

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()
