"""Payload requirements follow the runtime readers, not the packaging lists.

Two halves. The LABELLED half drives the real ``stage_release.py`` CLI over
``stage_release_smoke``'s committed throwaway fixture, reshaped per label, so
each label reaches the production staging path and reads only its output:

* ``empty_container_requires_leaf`` -- a non-empty payload root missing the
  leaf its installer reads is refused, naming the leaf and the reader;
* ``missing_leaf_names_reader`` -- a shipped module reading an absent
  package resource is refused, naming the module line and the resource;
* ``new_reader_changes_required_set`` -- a new ``sys.prefix`` reader changes
  the derived set with no packaging-list edit and yields named evidence;
* ``raw_read_is_uncovered`` -- a development-relative read of a derived leaf
  is an advisory, never a requirement or coverage;
* ``unlisted_mirror_is_reported`` -- a bundle mirror no list names is found
  by directory census and reported with its missing leaves.

The UNIT half exercises the derivation helper directly: the narrow resolution
forms the payload uses, the immutable-requirement lifecycle (correction,
retirement, reversal), and the leaf-not-parent archive rule. Every label runs
and reports; the smoke fails if any label fails. Temporary directories only;
no network, no real release staging.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from solet_setup_contracts.payload_requirements import Finding, MirrorRecord, ReaderCensus, Requirement

_ROOT = Path(__file__).resolve().parents[3]
assert (_ROOT / "solet_setup_contracts" / "src").is_dir(), _ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "solet_setup_contracts" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stage_release_smoke as stage_smoke  # noqa: E402

_PACKAGE = "solet_cli/src/solet_manager"
_PACKAGE_MARKER = {f"{_PACKAGE}/__init__.py": b""}
_FAILED: list[str] = []
_PASSED: list[str] = []


def _label(name: str, condition: bool, expected: str, actual: str) -> None:
    """Record one labelled assertion; every label runs whatever the others do."""
    if condition:
        _PASSED.append(name)
        print(f"  PASS  {name}")
        return
    _FAILED.append(name)
    print(f"  FAIL  {name}: expected {expected}; actual {actual}")


def _stage_fixture(root: Path, extra_files: dict[str, bytes], removed_files: tuple[str, ...] = ()) -> tuple[int, str]:
    """Stage one reshaped fixture through the real CLI; return (exit, output)."""
    root.mkdir(parents=True)
    checkout, manager_checkout, manager_ref = stage_smoke._build_seed_fixture(  # noqa: SLF001
        root, extra_files=extra_files, removed_files=removed_files
    )
    result = stage_smoke._stage(  # noqa: SLF001
        checkout, manager_checkout, manager_ref, root / "output", expect_success=False
    )
    return result.returncode, result.stdout + result.stderr


def _summary(code: int, output: str) -> str:
    tail = output.strip().splitlines()[-1:] or [""]
    return f"exit {code}, last line {tail[0][:220]!r}"


def _check_empty_container(scratch: Path) -> None:
    code, output = _stage_fixture(
        scratch / "empty-container",
        {"solet_setup_contracts/README": b"a container is not its contents\n"},
        removed_files=("solet_setup_contracts/pyproject.toml",),
    )
    _label(
        "empty_container_requires_leaf",
        code != 0 and "solet_setup_contracts/pyproject.toml" in output and "solet.rb.template:" in output,
        "refusal naming solet_setup_contracts/pyproject.toml and the Formula line that reads it",
        _summary(code, output),
    )


_MISSING_RESOURCE_READER = b"""from importlib import resources


def read_catalog() -> bytes:
    return resources.files("solet_manager").joinpath("released_metadata", "fixture_catalog.json").read_bytes()
"""


def _check_missing_leaf(scratch: Path) -> None:
    reader = f"{_PACKAGE}/fixture_resource_reader.py"
    code, output = _stage_fixture(scratch / "missing-leaf", {**_PACKAGE_MARKER, reader: _MISSING_RESOURCE_READER})
    _label(
        "missing_leaf_names_reader",
        code != 0 and f"{reader}:5" in output and "released_metadata/fixture_catalog.json" in output,
        f"refusal naming {reader}:5 and released_metadata/fixture_catalog.json",
        _summary(code, output),
    )


_NEW_PREFIX_READER = b"""import sys
from pathlib import Path


def read_extra_contract() -> bytes:
    return (Path(sys.prefix) / "share" / "solet" / "contracts" / "fixture_extra_contract.json").read_bytes()
"""


def _check_new_reader(scratch: Path) -> None:
    reader = f"{_PACKAGE}/fixture_contract_reader.py"
    extra = {
        **_PACKAGE_MARKER,
        reader: _NEW_PREFIX_READER,
        f"{stage_smoke._CONTRACT_ARCHIVE_ROOT}/fixture_extra_contract.json": b"{}\n",  # noqa: SLF001
    }
    code, output = _stage_fixture(scratch / "new-reader", extra)
    _label(
        "new_reader_changes_required_set",
        code != 0 and "share/solet/contracts/fixture_extra_contract.json" in output and f"{reader}:6" in output and "does not list fixture_extra_contract.json" in output,
        "the new reader's leaf is derived without a packaging-list edit and its omission from the installer is named",
        _summary(code, output),
    )


_RAW_AND_PRODUCTION_READERS = b"""import sys
from pathlib import Path


def production_flow() -> str:
    return (Path(sys.prefix) / "share" / "solet" / "contracts" / "macos_setup_flow.json").read_text(encoding="utf-8")


def development_flow() -> str:
    return (Path("plugins/github_midwife_plugin/knowledge_base") / "macos_setup_flow.json").read_text(encoding="utf-8")
"""


def _check_raw_read(scratch: Path) -> None:
    reader = f"{_PACKAGE}/fixture_flow_readers.py"
    code, output = _stage_fixture(scratch / "raw-read", {**_PACKAGE_MARKER, reader: _RAW_AND_PRODUCTION_READERS})
    advisory = f"payload advisory: uncovered_read at {reader}:10: reads macos_setup_flow.json"
    production = f"read at {reader}:6 via"
    raw_as_requirement = f"read at {reader}:10 via"
    _label(
        "raw_read_is_uncovered",
        code == 0 and advisory in output and production in output and raw_as_requirement not in output,
        "the development-relative read is an uncovered advisory while the sys.prefix read is the requirement",
        _summary(code, output),
    )


def _check_unlisted_mirror(scratch: Path) -> None:
    mirror = "solet_cli/tests/fixtures/unlisted_bundle"
    source = stage_smoke._CONTRACT_SOURCE  # noqa: SLF001
    extra = {
        f"{mirror}/macos_setup_flow.json": (source / "macos_setup_flow.json").read_bytes(),
        f"{mirror}/setup_flow.schema.json": (source / "setup_flow.schema.json").read_bytes(),
    }
    code, output = _stage_fixture(scratch / "unlisted-mirror", extra)
    reported = f"payload mirror: {mirror} carries 2 bundle leaves, missing: "
    line = next((item for item in output.splitlines() if item.startswith(reported)), "")
    _label(
        "unlisted_mirror_is_reported",
        code == 0 and "permissions_manifest.json" in line and "payload canonical: plugins/github_midwife_plugin/knowledge_base" in output,
        f"a mirror line for {mirror} naming permissions_manifest.json among its missing leaves",
        _summary(code, output) if not line else line,
    )


def _run_labels() -> None:
    with tempfile.TemporaryDirectory(prefix="payload-requirements-") as raw:
        scratch = Path(raw)
        _check_empty_container(scratch)
        _check_missing_leaf(scratch)
        _check_new_reader(scratch)
        _check_raw_read(scratch)
        _check_unlisted_mirror(scratch)


# --- Unit half: the derivation helper itself ----------------------------------------


_FORMULA = """  def install
    venv.pip_install buildpath/"dist", build_isolation: false
    (libexec/"share"/"solet"/"contracts").install Dir[
      "bundle/flow.json",
      "bundle/manifest.json",
    ]
    (libexec/"share"/"solet"/"lock.json").write <<~'JSON'
    JSON
  end
"""
_PYPROJECT = b'[tool.setuptools.packages.find]\nwhere = ["src"]\n[tool.setuptools.package-data]\npkg = ["data/*.json"]\n'
_PROVIDERS = b"""_FLOW = "flow.json"
_NAMES = (_FLOW, "manifest.json")


def bundle_names() -> tuple[str, ...]:
    return _NAMES
"""
_READERS = b"""import sys
from importlib import resources
from pathlib import Path

from .names import _NAMES, bundle_names


def _require(directory: Path) -> None:
    missing = [name for name in _NAMES if not (directory / name).is_file()]
    assert not missing


def contracts(explicit: Path | None) -> Path:
    candidate = explicit if explicit is not None else Path(sys.prefix) / "share" / "solet" / "contracts"
    _require(candidate)
    return candidate


def package_bytes(name: str) -> bytes:
    return resources.files("pkg").joinpath("data", name).read_bytes()


def table() -> bytes:
    return package_bytes("table.json") + (Path(__file__).with_name("data") / "notes.json").read_bytes()


def lock() -> bytes:
    return (Path(sys.prefix) / "share" / "solet" / "lock.json").read_bytes()


def provided() -> list[Path]:
    return [Path(sys.prefix) / "share" / "solet" / "contracts" / name for name in bundle_names()]


def dynamic(module: str) -> Path:
    return Path(__import__(module).__file__ or "")


def raw_flow() -> str:
    return (Path("bundle") / "flow.json").read_text()
"""


def _members(readers: bytes = _READERS) -> dict[str, bytes]:
    return {
        "dist/pyproject.toml": _PYPROJECT,
        "dist/src/pkg/__init__.py": b"",
        "dist/src/pkg/names.py": _PROVIDERS,
        "dist/src/pkg/readers.py": readers,
        "dist/src/pkg/data/table.json": b"{}",
        "dist/src/pkg/data/notes.json": b"{}",
    }


_READERS_MODULE = "dist/src/pkg/readers.py"


def _load_stager() -> ModuleType:
    """The real ``stage_release.py`` as a module, loaded by path as ``release_world`` does."""
    spec = importlib.util.spec_from_file_location("payload_requirements_smoke_stage_release", stage_smoke._SCRIPT)  # noqa: SLF001
    if spec is None or spec.loader is None:
        raise AssertionError("red: cannot load stage_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _derive(stager: ModuleType, members: dict[str, bytes]) -> Any:  # noqa: ANN401 -- the stager's PayloadEvidence
    """Map a synthetic payload through the stager's own adapter and Formula parser."""
    return stager._derive_requirements(members, stager._parse_formula(_FORMULA, "Formula.rb"), {})  # noqa: SLF001


def _installed(evidence: Any, reader_prefix: str = _READERS_MODULE) -> set[str]:  # noqa: ANN401
    return {item.installed_path for item in evidence.requirements if item.reader.startswith(reader_prefix)}


def _check_census(helper: ModuleType) -> None:
    """What the helper alone reports: anchored reads, advisories and raw reads."""
    census: ReaderCensus = helper.census_readers(_members(), ["dist/src"])
    reads = {(read.anchor, read.path) for read in census.reads if read.location.startswith(_READERS_MODULE)}
    expected = {
        ("sys.prefix", "share/solet/contracts"),
        ("sys.prefix", "share/solet/contracts/flow.json"),
        ("sys.prefix", "share/solet/contracts/manifest.json"),
        ("sys.prefix", "share/solet/lock.json"),
        ("package_source", "dist/src/pkg/data/table.json"),
        ("package_source", "dist/src/pkg/data/notes.json"),
    }
    _label("unit:census_resolution_forms", reads == expected, f"{sorted(expected)}", f"{sorted(reads)}")
    kinds = {item.kind for item in census.findings if item.location.startswith(_READERS_MODULE)}
    _label("unit:census_findings", kinds == {"unresolved_anchor", "caller_alternative"}, "dynamic module file unresolved, explicit alternative recorded", f"{sorted(kinds)}")
    raw_line = next(number for number, text in enumerate(_READERS.decode().splitlines(), start=1) if 'Path("bundle")' in text)
    uncovered: tuple[Finding, ...] = helper.uncovered_reads(census.raw_reads, frozenset({"flow.json"}))
    locations = [item.location for item in uncovered]
    _label("unit:raw_read_of_a_derived_leaf_is_uncovered", locations == [f"{_READERS_MODULE}:{raw_line}"], f"only the development-relative flow.json read at line {raw_line}", f"{locations}")


def _check_resolution(stager: ModuleType) -> None:
    derivation = _derive(stager, _members())
    installed = _installed(derivation)
    expected = {
        "share/solet/contracts/flow.json",
        "share/solet/contracts/manifest.json",
        "share/solet/lock.json",
        "site-packages/pkg/data/table.json",
        "site-packages/pkg/data/notes.json",
    }
    _label("unit:resolution_forms", installed == expected, f"{sorted(expected)}", f"{sorted(installed)}")
    provider_lines = {item.reader for item in derivation.requirements if item.installed_path == "share/solet/contracts/manifest.json" and item.reader.startswith("dist/src/pkg/readers.py")}
    _label(
        "unit:wrapper_and_provider_both_resolve",
        len(provider_lines) == 2,
        "manifest.json derived once through the _require() wrapper and once through bundle_names()",
        f"{sorted(provider_lines)}",
    )
    kinds = {(item.kind, item.location.rsplit(":", 1)[0]) for item in derivation.findings}
    _label(
        "unit:advisories_and_alternatives",
        ("unresolved_anchor", "dist/src/pkg/readers.py") in kinds and ("uncovered_read", "dist/src/pkg/readers.py") in kinds and ("caller_alternative", "dist/src/pkg/readers.py") in kinds,
        "dynamic module file unresolved, raw bundle read uncovered, explicit alternative recorded",
        f"{sorted(kinds)}",
    )
    failures = [item.describe() for item in derivation.failures]
    _label("unit:complete_tree_has_no_failures", not failures, "no failures", f"{failures}")


def _check_lifecycle(stager: ModuleType) -> None:
    original = _derive(stager, _members())
    corrected = _derive(stager, _members(_READERS.replace(b'"notes.json"', b'"renamed.json"')))
    retired = _derive(stager, _members(_READERS.replace(b'(Path(__file__).with_name("data") / "notes.json").read_bytes()', b"b''")))
    reversed_ = _derive(stager, _members())
    corrected_set = _installed(corrected)
    _label(
        "unit:correction_is_a_new_derivation",
        "site-packages/pkg/data/renamed.json" in corrected_set and "site-packages/pkg/data/notes.json" not in corrected_set,
        "the corrected reader's leaf replaces the old one",
        f"{sorted(corrected_set)}",
    )
    _label(
        "unit:retirement_removes_the_requirement",
        "site-packages/pkg/data/notes.json" not in _installed(retired),
        "no requirement once its reader is gone",
        f"{sorted(_installed(retired))}",
    )
    _label(
        "unit:reversal_restores_the_derivation",
        reversed_ == original,
        "restoring the reader restores the identical derivation",
        "derivations differ",
    )


def _check_archive_rule(helper: ModuleType, stager: ModuleType) -> None:
    evidence = _derive(stager, _members())
    regular = frozenset(_members()) | {"bundle/flow.json", "bundle/other.json"}
    verified: tuple[Requirement, ...] = helper.verify_archive(evidence.requirements, regular)
    missing = {item.source_leaf for item in verified if item.coverage == "missing"}
    _label(
        "unit:parent_directory_never_satisfies_a_leaf",
        missing == {"bundle/manifest.json"},
        "bundle/manifest.json missing although bundle/ holds other regular files",
        f"{sorted(str(item) for item in missing)}",
    )
    names = [*regular, "elsewhere/flow.json"]
    mirrors: tuple[MirrorRecord, ...] = helper.census_mirrors(names, frozenset({"flow.json", "manifest.json"}), frozenset({"bundle"}))
    found = {(item.directory, item.canonical, item.missing) for item in mirrors}
    _label(
        "unit:census_finds_unlisted_directories",
        found == {("bundle", True, ("manifest.json",)), ("elsewhere", False, ("manifest.json",))},
        "the canonical bundle and the unlisted mirror, each with its missing leaf",
        f"{sorted(found)}",
    )


def _run_units() -> None:
    """Imported here, not at module top, so the labelled half also runs where the helper is absent."""
    try:
        helper = importlib.import_module("solet_setup_contracts.payload_requirements")
    except ImportError as exc:
        _label("unit:helper_importable", False, "the derivation helper imports", f"{exc}")
        return
    stager = _load_stager()
    _check_census(helper)
    _check_resolution(stager)
    _check_lifecycle(stager)
    _check_archive_rule(helper, stager)


def main() -> int:
    print("=== payload_requirements_smoke ===")
    _run_labels()
    _run_units()
    print(f"payload_requirements_smoke: {len(_PASSED)} passed, {len(_FAILED)} failed")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
