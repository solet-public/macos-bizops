"""Installed-layout loader coverage for the derived payload requirements.

Stages a throwaway committed fixture carrying this checkout's REAL manager
sources (``solet_cli`` and ``solet_setup_contracts``) through the real
``stage_release.py``, rebuilds the installed layout from the staged archive
through the Formula mapping the stager reported, and runs the actual
production entry -- ``ContractBundle.load`` with no directory, which resolves
``sys.prefix/share/solet/contracts`` -- in an isolated interpreter whose
``solet_manager`` is the archived copy, never an editable install.

* ``installed_loader_rejects_omission``: the contract leaves the stager
  derives from ContractBundle's own reader equal the loader's required set;
  the complete installed layout loads with origin ``production``; removing
  each derived leaf in turn makes the production loader refuse, naming it.
* an explicit-directory load reports origin ``explicit``: development reads
  are never production coverage.
* every bundle directory below the code roots is found by directory census and run
  through the same production loader. A rejected mirror is reported
  UNCOVERED -- evidence, not a failure, because fixture roster parity is a
  separate root cause.
* every test call of ``ContractBundle.load`` is classified by the path it
  reads through: production (no directory) or explicit (a directory).

Temporary directories only; no network, no real release staging.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parents[2]
assert (_ROOT / "solet_cli" / "homebrew" / "tests").is_dir(), _ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "solet_setup_contracts" / "src"))
sys.path.insert(0, str(_ROOT / "solet_cli" / "homebrew" / "tests"))

import stage_release_smoke as stage_smoke  # noqa: E402
from solet_manager.contracts import contract_filenames  # noqa: E402

_SHIPPED = ("solet_cli/src", "solet_setup_contracts/src", "solet_cli/pyproject.toml", "solet_setup_contracts/pyproject.toml")
_CONTRACTS_DIR = "share/solet/contracts"
_LOADER_READER = "solet_cli/src/solet_manager/contracts.py:"
_TEST_ROOTS = ("solet_cli/tests", "solet_cli/homebrew/tests", "quality_gates/tests")
# Where a materialized bundle mirror can live: the code roots, never data trees.
_CODE_ROOTS = ("ananta", "plugins", "solet_cli", "solet_setup_contracts", "quality_gates")
_REQUIREMENT = re.compile(
    rf"^payload requirement: {_CONTRACTS_DIR}/(?P<leaf>\S+) <- (?P<source>\S+) read at (?P<reader>\S+) via .*\[(?P<coverage>\w+)\]$",
    re.MULTILINE,
)
_LOADER = """
import json, sys
prefix, *paths = sys.argv[1:]
sys.path[:0] = paths
sys.prefix = prefix
import solet_manager.contracts as contracts
from solet_manager.errors import ContractError
explicit = prefix + "/share/solet/contracts" if "--explicit" in sys.argv else None
try:
    bundle = contracts.ContractBundle.load(source_revision="fixture", directory=None if explicit is None else __import__("pathlib").Path(explicit))
except ContractError as exc:
    print(json.dumps({"ok": False, "error": str(exc), "module": contracts.__file__}))
else:
    print(json.dumps({"ok": True, "origin": bundle.resolution.origin, "module": contracts.__file__}))
"""
_FAILED: list[str] = []
_PASSED: list[str] = []


def _label(name: str, condition: bool, expected: str, actual: str) -> None:
    if condition:
        _PASSED.append(name)
        print(f"  PASS  {name}")
        return
    _FAILED.append(name)
    print(f"  FAIL  {name}: expected {expected}; actual {actual}")


def _files(*paths: str) -> list[str]:
    """Repository-relative regular files at or below ``paths``, without Git.

    A walk rather than ``git ls-files``, so the smoke measures an archive
    export exactly as it measures a checkout. Hidden directories, bytecode and
    vendored ``node_modules`` are never part of a payload, so they are pruned.
    """
    found: list[str] = []
    for path in paths:
        if (_ROOT / path).is_file():
            found.append(path)
            continue
        for directory, subdirectories, filenames in os.walk(_ROOT / path):
            subdirectories[:] = sorted(item for item in subdirectories if not item.startswith(".") and item not in ("__pycache__", "node_modules"))
            found.extend(Path(directory, name).relative_to(_ROOT).as_posix() for name in sorted(filenames))
    return found


def _real_sources() -> dict[str, bytes]:
    return {name: (_ROOT / name).read_bytes() for name in _files(*_SHIPPED)}


def _run_loader(prefix: Path, extracted: Path, *, explicit: bool = False) -> dict[str, object]:
    """Run the production loader in an interpreter that can import only the archived copy.

    ``-I`` drops the environment and ``-S`` drops site-packages altogether, so
    no editable install and no battery import-environment finder can supply
    ``solet_manager``; the loader needs nothing outside the standard library
    and the two archived distributions.
    """
    arguments = [sys.executable, "-I", "-S", "-c", _LOADER, str(prefix), str(extracted / "solet_cli" / "src"), str(extracted / "solet_setup_contracts" / "src")]
    result = subprocess.run([*arguments, *(["--explicit"] if explicit else [])], check=False, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not result.stdout.strip():
        return {"ok": False, "error": f"loader exited {result.returncode}: {result.stderr.strip()[-400:]}", "module": ""}
    payload: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
    return payload


def _install_contracts(prefix: Path, extracted: Path, mapping: dict[str, str]) -> Path:
    """Place each installed leaf the stager mapped, from the archive, below ``prefix``."""
    contracts = prefix / _CONTRACTS_DIR
    shutil.rmtree(prefix, ignore_errors=True)
    contracts.mkdir(parents=True)
    for leaf, source in mapping.items():
        shutil.copyfile(extracted / source, contracts / leaf)
    return contracts


def _stage_real_sources(scratch: Path) -> tuple[str, Path]:
    scratch.mkdir(parents=True)
    checkout, manager_checkout, manager_ref = stage_smoke._build_seed_fixture(scratch, extra_files=_real_sources())  # noqa: SLF001
    output = scratch / "output"
    result = stage_smoke._stage(checkout, manager_checkout, manager_ref, output, expect_success=False)  # noqa: SLF001
    if result.returncode != 0:
        raise AssertionError(f"red: staging the real manager sources failed: {result.stderr.strip()[-800:]}")
    extracted = scratch / "extracted"
    with tarfile.open(stage_smoke._payload_archive_path(output), mode="r:gz") as archive:  # noqa: SLF001
        archive.extractall(extracted, filter="data")
    return result.stdout, extracted


def _check_installed_loader(stdout: str, extracted: Path, scratch: Path) -> None:
    rows = [match.groupdict() for match in _REQUIREMENT.finditer(stdout)]
    mapping = {row["leaf"]: row["source"] for row in rows if row["coverage"] == "archive_member"}
    derived = {row["leaf"] for row in rows if row["reader"].startswith(_LOADER_READER)}
    required = set(contract_filenames())
    _label(
        "installed_loader_rejects_omission:derived_set",
        derived == required,
        f"the stager derives exactly ContractBundle's required leaves from its reader {sorted(required)}",
        f"{sorted(derived)}",
    )
    if derived:
        _check_layout_loads_and_omissions_refuse(derived, mapping, extracted, scratch / "prefix")


def _check_layout_loads_and_omissions_refuse(derived: set[str], mapping: dict[str, str], extracted: Path, prefix: Path) -> None:
    """The production entry loads the complete layout, refuses each omission, and an explicit load is not production."""
    _install_contracts(prefix, extracted, mapping)
    complete = _run_loader(prefix, extracted)
    archived_module = str(complete.get("module", "")).startswith(str(extracted))
    _label(
        "installed_loader_rejects_omission:complete_layout_loads",
        complete.get("ok") is True and complete.get("origin") == "production" and archived_module,
        "the archived solet_manager loads the complete installed layout with origin production",
        f"{complete}",
    )
    for leaf in sorted(derived):
        (_install_contracts(prefix, extracted, mapping) / leaf).unlink()
        refused = _run_loader(prefix, extracted)
        _label(
            f"installed_loader_rejects_omission:{leaf}",
            refused.get("ok") is False and leaf in str(refused.get("error")),
            f"the production loader refuses the layout without {leaf}, naming it",
            f"{refused}",
        )
    _install_contracts(prefix, extracted, mapping)
    explicit = _run_loader(prefix, extracted, explicit=True)
    _label(
        "explicit_load_is_not_production_coverage",
        explicit.get("ok") is True and explicit.get("origin") != "production",
        "an explicit-directory load reports a non-production origin",
        f"{explicit}",
    )


def _check_mirrors(helper: ModuleType, mapping_names: frozenset[str], extracted: Path, scratch: Path) -> None:
    canonical = frozenset({stage_smoke._CONTRACT_ARCHIVE_ROOT})  # noqa: SLF001
    mirrors = helper.census_mirrors(_files(*_CODE_ROOTS), mapping_names, canonical)
    verdicts: dict[str, bool] = {}
    for mirror in mirrors:
        prefix = scratch / "mirror-prefix"
        contracts = _install_contracts(prefix, extracted, {})
        for leaf in mirror.present:
            shutil.copyfile(_ROOT / mirror.directory / leaf, contracts / leaf)
        verdict = _run_loader(prefix, extracted)
        verdicts[mirror.directory] = verdict.get("ok") is True
        status = "production loader PASS" if verdict.get("ok") is True else f"UNCOVERED: {str(verdict.get('error'))[:160]}"
        print(f"  mirror {'canonical' if mirror.canonical else 'fixture'} {mirror.directory}: missing {list(mirror.missing)}; {status}")
    _label(
        "mirror_census_runs_every_mirror_through_the_loader",
        verdicts.get(stage_smoke._CONTRACT_ARCHIVE_ROOT) is True and len(verdicts) == len(mirrors),  # noqa: SLF001
        "every bundle directory below the code roots is found by census and the canonical one loads in production",
        f"{len(verdicts)} mirrors, canonical {verdicts.get(stage_smoke._CONTRACT_ARCHIVE_ROOT)}",  # noqa: SLF001
    )


def _is_bundle_load(node: ast.AST) -> bool:
    """``ContractBundle.load(...)``: the loader entry a test can call."""
    func = node.func if isinstance(node, ast.Call) else None
    return isinstance(func, ast.Attribute) and func.attr == "load" and isinstance(func.value, ast.Name) and func.value.id == "ContractBundle"


def _passes_directory(call: ast.Call) -> bool:
    """Whether the call passes a non-``None`` ``directory=``: the explicit, development path."""
    return any(keyword.arg == "directory" and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None) for keyword in call.keywords)


def _load_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_bundle_load(node)]
    return [(call.lineno, "explicit" if _passes_directory(call) else "production") for call in calls]


def _check_test_loader_paths() -> None:
    counts = {"explicit": 0, "production": 0}
    for name in _files(*_TEST_ROOTS):
        if name.endswith(".py"):
            for _line, kind in _load_calls(_ROOT / name):
                counts[kind] += 1
    print(f"  test ContractBundle.load calls: {counts['production']} through the production entry, {counts['explicit']} through an explicit directory (not production coverage)")
    _label("test_loader_paths_are_classified", sum(counts.values()) > 0, "at least one classified test loader call", f"{counts}")


def main() -> int:
    print("=== payload_reader_coverage_smoke ===")
    with tempfile.TemporaryDirectory(prefix="payload-reader-coverage-") as raw:
        scratch = Path(raw)
        stdout, extracted = _stage_real_sources(scratch / "stage")
        _check_installed_loader(stdout, extracted, scratch)
        try:
            helper = importlib.import_module("solet_setup_contracts.payload_requirements")
        except ImportError as exc:
            _label("mirror_census_runs_every_mirror_through_the_loader", False, "the derivation helper imports", f"{exc}")
        else:
            names = frozenset(match.group("leaf") for match in _REQUIREMENT.finditer(stdout))
            _check_mirrors(helper, names, extracted, scratch)
    _check_test_loader_paths()
    print(f"payload_reader_coverage_smoke: {len(_PASSED)} passed, {len(_FAILED)} failed")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
