"""Finder-editable isolation through real, disposable virtualenvs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from quality_gates import candidate_repo  # noqa: E402
from quality_gates.smoke_import_environment import ImportOriginError, SmokeImportEnvironment  # noqa: E402

FINDER = "__editable___fixture_1_0_finder"
PTH = "__editable__.fixture-1.0.pth"
IMPORT_LINE = f"import {FINDER}; {FINDER}.install()\n"
FINDER_CODE = """import sys
from importlib.util import spec_from_file_location
from pathlib import Path
MAPPING: dict[str, str] = MAPPING_VALUE
class Finder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if fullname in MAPPING:
            return spec_from_file_location(fullname, Path(MAPPING[fullname]) / '__init__.py')
def install():
    sys.meta_path.append(Finder)
"""


def _finder(mapping: dict[str, str]) -> str:
    return FINDER_CODE.replace("MAPPING_VALUE", repr(mapping))


def _site(root: Path) -> Path:
    return next(root.glob("lib/python3.*/site-packages"))


def _package(root: Path, relative: str, value: str) -> Path:
    directory = root / relative
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
    return directory


class FinderSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.source = self.workspace / "source"
        self.target = self.workspace / "candidate"
        self.shared = self.source / "site"
        self.site = self.target / "site"
        self.shared.mkdir(parents=True)
        self.site.mkdir(parents=True)
        self.mapping = {"fixture": str(self.source / "src/fixture"), "fixture.vendor": str(self.source / "vendor")}
        self.finder = self.shared / (FINDER + ".py")
        self.finder.write_text(_finder(self.mapping), encoding="utf-8")
        (self.shared / PTH).write_text(IMPORT_LINE, encoding="utf-8")

    def install(self) -> None:
        candidate_repo._install_site_packages(self.shared, self.site, self.source, self.target)

    def test_private_interpreter_prefix(self) -> None:
        _assert_private_prefix()

    def test_private_environment_lifecycle(self) -> None:
        _assert_private_lifecycle()

    def test_cleanup_after_supervised_descendants(self) -> None:
        _assert_cleanup_after_supervised_exit()

    def test_provision_rehomes_finder_and_vendor(self) -> None:
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.source / ".venv")], check=True)
        shared_site = _site(self.source / ".venv")
        for entry in self.shared.iterdir():
            (shared_site / entry.name).write_bytes(entry.read_bytes())
        for root, value in [(self.source, "shared"), (self.target, "candidate")]:
            _package(root, "src/fixture", value)
            _package(root, "vendor", value)
        original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in shared_site.glob("__editable__*")}
        venv = candidate_repo.provision_candidate_venv(self.source, self.target)
        site = _site(venv)
        self.assertFalse((site / (FINDER + ".py")).is_symlink())
        self.assertEqual((site / PTH).read_bytes(), IMPORT_LINE.encode())
        probe = subprocess.run([str(venv / "bin/python3"), "-c", "import json,fixture,fixture.vendor; print(json.dumps([fixture.__file__, fixture.vendor.__file__, fixture.VALUE, fixture.vendor.VALUE]))"], cwd=self.target, capture_output=True, text=True, check=True)
        paths = json.loads(probe.stdout)
        self.assertEqual(paths[2:], ["candidate", "candidate"])
        for path in paths[:2]:
            self.assertTrue(Path(path).resolve().is_relative_to(self.target), path)
        self.assertEqual(original, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in shared_site.glob("__editable__*")})

    def test_existing_symlink_never_writes_shared_finder(self) -> None:
        original = self.finder.read_bytes()
        (self.site / self.finder.name).symlink_to(self.finder)
        self.install()
        self.assertEqual(self.finder.read_bytes(), original)
        copied = self.site / self.finder.name
        self.assertFalse(copied.is_symlink())
        self.assertIn(str(self.target / "vendor"), copied.read_text())
        self.assertNotIn(str(self.source), copied.read_text())

    def test_malformed_mapping_is_named(self) -> None:
        for expression in ["make_mapping()", "{}", "{'fixture': 123}", "['bad']"]:
            with self.subTest(expression=expression):
                self.finder.write_text(FINDER_CODE.replace("MAPPING_VALUE", expression))
                with self.assertRaisesRegex(candidate_repo.CandidateRepoError, self.finder.name):
                    self.install()
                self.assertFalse((self.site / PTH).exists())

    def test_missing_and_duplicate_mapping_refuse(self) -> None:
        for payload in ["def broken(", "VALUE = {}\n", "MAPPING = {}\nMAPPING = {}\n"]:
            with self.subTest(payload=payload):
                self.finder.write_text(payload)
                with self.assertRaisesRegex(candidate_repo.CandidateRepoError, self.finder.name):
                    self.install()

    def test_source_escape_refuses(self) -> None:
        for path in ["relative/path", str(self.workspace / "source_sibling/pkg"), str(self.source / "../elsewhere")]:
            with self.subTest(path=path):
                self.finder.write_text(_finder({"fixture": path}))
                with self.assertRaisesRegex(candidate_repo.CandidateRepoError, "outside shared source root"):
                    self.install()

    def test_source_symlink_escape_refuses(self) -> None:
        (self.source / "vendor").symlink_to(self.workspace)
        with self.assertRaisesRegex(candidate_repo.CandidateRepoError, "outside shared source root"):
            self.install()

    def test_candidate_symlink_escape_refuses(self) -> None:
        (self.target / "vendor").symlink_to(self.source)
        with self.assertRaisesRegex(candidate_repo.CandidateRepoError, "inside the candidate root"):
            self.install()

    def test_import_bytes_and_plain_mapping_preserved(self) -> None:
        raw = IMPORT_LINE.replace("\n", "\r\n").encode()
        (self.shared / PTH).write_bytes(raw)
        self.finder.write_text(_finder(self.mapping).replace("MAPPING: dict[str, str]", "MAPPING"))
        self.install()
        self.assertEqual((self.site / PTH).read_bytes(), raw)
        self.assertEqual((self.shared / PTH).read_bytes(), raw)

    def test_missing_finder_refuses_with_entry(self) -> None:
        self.finder.unlink()
        with self.assertRaisesRegex(candidate_repo.CandidateRepoError, self.finder.name):
            self.install()

    def test_unrecognized_import_stays_refused(self) -> None:
        (self.shared / PTH).write_text("import os; os.getcwd()\n")
        with self.assertRaisesRegex(candidate_repo.CandidateRepoError, "not an absolute path"):
            self.install()


_DESCENDANT = """import json, sys
from pathlib import Path
from importlib.resources import files
import macos_inference_plugin, origin_dependency
resource_origins = {}
for name in ('resources', 'vendor'):
    try:
        package = files('macos_inference_plugin.' + name)
        resource_origins[name] = dict(path=str(package), bytes=package.joinpath('marker.txt').read_text())
    except ModuleNotFoundError:
        resource_origins[name] = dict(path=None, bytes=None)
print(json.dumps(dict(origin=str(Path(macos_inference_plugin.__file__).resolve()),
 marker=macos_inference_plugin.MARKER, prefix=sys.prefix, executable=sys.executable,
 dependency=origin_dependency.MARKER, resources=resource_origins)))
"""


def _origin_layout(workspace: Path, *, wheel: bool = False) -> tuple[Path, Path]:
    """Model the shared editable owner and a different runner's package bytes."""
    shared, runner = workspace / "shared", workspace / "runner"
    package_name = "macos_inference_plugin"
    shared_plugin = shared / "plugins" / package_name
    for tree, marker in ((shared, "SHARED"), (runner, "RUNNER")):
        _package(tree, "ananta/src/ananta", "fixture")
        plugin = tree / "plugins" / package_name
        package = plugin / "src" / package_name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"MARKER = {marker!r}\n")
        (package / "__main__.py").write_text(_DESCENDANT)
        for directory in (package / "resources", plugin / "vendor"):
            directory.mkdir()
            (directory / "__init__.py").write_text("")
            (directory / "marker.txt").write_text(marker)
        (plugin / "pyproject.toml").write_text('[tool.setuptools]\npackages = ["macos_inference_plugin", "macos_inference_plugin.resources", "macos_inference_plugin.vendor"]\n[tool.setuptools.package-dir]\n"" = "src"\n"macos_inference_plugin.vendor" = "vendor"\n')
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(shared / ".venv")], cwd=workspace, check=True, capture_output=True, text=True)
    installed = next((shared / ".venv").glob("lib/python3.*/site-packages"))
    dependency = installed / "origin_dependency"
    dependency.mkdir()
    (dependency / "__init__.py").write_text("MARKER = 'DEPENDENCY'\n")
    roots = [str(shared / "ananta/src")]
    if not wheel:
        roots.append(str(shared_plugin / "src"))
    (installed / "__editable__.ananta.pth").write_text("\n".join(roots) + "\n")
    if wheel:
        shutil.copytree(shared_plugin / "src" / package_name, installed / package_name)
        shutil.copytree(shared_plugin / "vendor", installed / package_name / "vendor")
    else:
        mapping = {package_name: str(shared_plugin / "src" / package_name), f"{package_name}.vendor": str(shared_plugin / "vendor")}
        (installed / (FINDER + ".py")).write_text(_finder(mapping))
        (installed / PTH).write_text(IMPORT_LINE)
    # A previous private-site build may have linked these entries. Replacing
    # them must unlink first, never write into the modeled shared environment.
    (installed / "__editable___runner_owned_finder.py").write_text("# shared sentinel\n")
    (installed / "__editable__.000_runner_owned.pth").write_text("\n")
    (runner / ".venv").symlink_to(shared / ".venv", target_is_directory=True)
    return shared, runner


def _shared_bytes(root: Path) -> dict[str, str]:
    """Hash fixture source and environment bytes, excluding interpreter caches."""
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts}


def _assert_private_lifecycle() -> None:
    """Create once, verify, correct target, reject stale owners and reverse."""

    from quality_gates import candidate_repo
    from quality_gates.smoke_import_environment import SmokeImportEnvironment

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        shared, runner = _origin_layout(workspace)
        before = _shared_bytes(shared)
        scratch = workspace / "scratch"
        scratch.mkdir()
        original = runner / ".venv/bin/python3"
        environment = SmokeImportEnvironment(runner, original, scratch)
        with patch.object(candidate_repo, "provision_private_venv", wraps=candidate_repo.provision_private_venv) as provision:
            with environment:
                first_receipt = dict(environment.receipt)
                environment.prepare()
                assert provision.call_count == 1, "PRIVATE_ENVIRONMENT_CREATED_ONCE"
                _assert_stale_owner(environment, runner, original, scratch)
                _assert_target_correction(environment, runner, first_receipt)
                assert provision.call_count == 2, "CHANGED_TARGET_REBUILDS_OWNED_ENVIRONMENT"
            assert not (scratch / "import-environment").exists(), "OWNED_ENVIRONMENT_REMOVED_AFTER_EXIT"
        assert environment.python == original, "ORIGINAL_INTERPRETER_RESTORED"
        result = subprocess.run([str(original), "-c", _DESCENDANT], cwd=workspace, env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"}, capture_output=True, text=True, check=True)
        assert json.loads(result.stdout)["marker"] == "SHARED", "SHARED_ENVIRONMENT_REVERSAL"
        assert _shared_bytes(shared) == before, "SHARED_ENVIRONMENT_UNCHANGED"
        _assert_exception_cleanup(runner, original, scratch)


def _assert_stale_owner(environment: SmokeImportEnvironment, runner: Path, original: Path, scratch: Path) -> None:
    assert Path(str(environment.receipt["prefix"])) == environment.path, "PRIVATE_INTERPRETER_PREFIX"
    other = SmokeImportEnvironment(runner, original, scratch)
    try:
        other.prepare()
    except ImportOriginError as exc:
        assert "PRIVATE_ENVIRONMENT_OWNER_MISMATCH" in str(exc), exc
    else:
        raise AssertionError("STALE_BATTERY_ENVIRONMENT_REFUSED")


def _assert_target_correction(environment: SmokeImportEnvironment, runner: Path, previous: dict[str, object]) -> None:
    corrected = runner.parent / "corrected"
    shutil.copytree(runner, corrected, ignore=shutil.ignore_patterns(".venv", "__pycache__"))
    environment.root = corrected
    environment.prepare()
    assert environment.receipt["state"] == "verified", environment.receipt
    assert previous["runner_root"] == str(runner), "HISTORICAL_RECEIPT_UNCHANGED"
    assert environment.receipt["runner_root"] == str(corrected)


def _assert_exception_cleanup(runner: Path, original: Path, scratch: Path) -> None:
    """Context errors and interruption remove only the owned environment."""
    from quality_gates.smoke_import_environment import ImportOriginError, SmokeImportEnvironment

    for exception in (RuntimeError, KeyboardInterrupt):
        try:
            with SmokeImportEnvironment(runner, original, scratch):
                raise exception("OWNED_EXCEPTION_FIXTURE")
        except exception:
            pass
        assert not (scratch / "import-environment").exists(), "OWNED_ENVIRONMENT_REMOVED_AFTER_EXIT"
    sentinel = scratch / "import-environment"
    sentinel.symlink_to(original.parent.parent)
    try:
        with SmokeImportEnvironment(runner, original, scratch):
            raise AssertionError("WRONG_ROOT_WAS_REUSED")
    except ImportOriginError as exc:
        assert "PRIVATE_ENVIRONMENT_OWNER_MISMATCH" in str(exc), exc
    else:
        raise AssertionError("WRONG_ROOT_ENVIRONMENT_REFUSED")
    assert sentinel.is_symlink(), "FOREIGN_ENVIRONMENT_PRESERVED"
    sentinel.unlink()


def _assert_private_prefix() -> None:
    """The selected spelling must start the private venv, not its base binary."""

    from quality_gates.smoke_import_environment import ImportOriginError, SmokeImportEnvironment

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        _, runner = _origin_layout(workspace)
        scratch = workspace / "scratch"
        scratch.mkdir()
        error: ImportOriginError | None = None
        try:
            with SmokeImportEnvironment(runner, runner / ".venv/bin/python3", scratch) as environment:
                result = subprocess.run([str(environment.python), "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True, check=True)
                assert Path(result.stdout.strip()) == environment.path, "PRIVATE_INTERPRETER_PREFIX"
        except ImportOriginError as exc:
            error = exc
        assert error is None, ("PRIVATE_INTERPRETER_PREFIX", str(error))
        assert not (scratch / "import-environment").exists(), "OWNED_ENVIRONMENT_REMOVED_AFTER_EXIT"


def _assert_missing_setup_advises(workspace: Path, runner: Path) -> None:
    """Missing scratch/provisioning retains a disclosed working original route."""
    from quality_gates import smoke_import_environment as imports

    empty = workspace / "empty"
    empty.mkdir()
    with imports.SmokeImportEnvironment(empty, Path(sys.executable), None) as environment:
        assert environment.python == Path(sys.executable)
        assert environment.receipt["state"] == "advisory" and environment.receipt["private"] is False
    shared = (runner / ".venv").resolve().parent
    with patch.object(imports.candidate_repo, "provision_private_venv", side_effect=OSError("PROVISION_UNAVAILABLE")):
        with imports.SmokeImportEnvironment(shared, shared / ".venv/bin/python3", workspace / "scratch") as environment:
            assert environment.python == shared / ".venv/bin/python3"
            assert environment.receipt["state"] == "advisory", environment.receipt
    with patch.object(imports, "probe_origins", return_value={"prefix": str(shared / ".venv"), "origins": {"ananta": str(shared / "ananta/src/ananta/__init__.py")}}):
        try:
            with imports.SmokeImportEnvironment(runner, runner / ".venv/bin/python3", workspace / "scratch"):
                raise AssertionError("KNOWN_WRONG_ORIGIN_WAS_ACCEPTED")
        except imports.ImportOriginError as exc:
            assert "OWNED_IMPORT_ORIGIN_IS_NOT_RUNNER_TREE" in str(exc), exc


def _assert_cleanup_after_supervised_exit() -> None:
    """A lingering real group is reaped while its private interpreter exists."""
    from _run_smokes_scratch_fixture import _group_alive

    from quality_gates import run_smokes

    original_close = SmokeImportEnvironment.close
    closed: list[Path] = []

    def finish(environment: SmokeImportEnvironment) -> None:
        supervisor = run_smokes._scratch_runtime().select_supervisor(lambda: run_smokes._SmokeProcessSupervisor(100, 1))
        groups, _ = supervisor.cancel_snapshot()
        assert groups and not any(_group_alive(group) for group in groups), "OWNED_ENVIRONMENT_REMOVED_AFTER_SUPERVISED_EXIT"
        assert environment.path is not None
        closed.append(environment.path)
        original_close(environment)

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        _, tree = _origin_layout(workspace)
        child = tree / "child.py"
        child.write_text("import subprocess,sys\nsubprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")
        register = workspace / "register"
        register.write_text(str(child) + "\n")
        with patch.object(run_smokes, "_REPO_ROOT", tree), patch.object(sys, "argv", ["runner", "--register", str(register), "--jobs", "1"]), patch.object(SmokeImportEnvironment, "close", finish):
            assert run_smokes.main() == 0
        assert closed and all(not path.exists() for path in closed), "OWNED_ENVIRONMENT_REMOVED_AFTER_EXIT"


if __name__ == "__main__":
    unittest.main()
