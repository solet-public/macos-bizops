#!/usr/bin/env python3
"""Check Ananta's declared metadata against wheel and editable installs."""

from __future__ import annotations

import argparse
import configparser
import email.parser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PROJECT_ROOT = _REPO_ROOT / "ananta"
_EXPECTED_WHEEL_MODULES = {
    "ananta/__init__.py",
    "ananta/cli.py",
    "ananta/core/orchestration/__init__.py",
}
_EXPECTED_PROJECT: dict[str, Any] = {
    "name": "ananta",
    "version": "2.0.0",
    "description": "A flexible framework for AI-driven applications",
    "license": "Apache-2.0",
    "requires-python": "==3.13.*",
    "dependencies": [
        "requests>=2.32.0",
        "python-dotenv>=1.0.0",
        "click>=8.0.0",
        "solet-setup-contracts",
        "pyyaml>=6.0",
        "jsonschema>=4.0",
        "pynacl>=1.5",
    ],
    "optional-dependencies": {
        "gate": ["ruff>=0.15", "pyright>=1.1", "radon>=6.0"],
    },
    "scripts": {"ananta": "ananta.cli:sync_main"},
    "classifiers": [
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
}


def _normalized(values: list[str]) -> list[str]:
    """Normalize metadata values while preserving duplicate requirements."""
    return sorted(" ".join(value.lower().split()) for value in values)


def _project_metadata(project_root: Path) -> dict[str, Any]:
    """Read the canonical PEP 621 metadata and reject a missing project table."""
    with (project_root / "pyproject.toml").open("rb") as handle:
        document: dict[str, Any] = tomllib.load(handle)
    project_value = document.get("project")
    if not isinstance(project_value, dict):
        raise AssertionError("metadata parity check: pyproject.toml has no [project] table")
    project = cast(dict[str, Any], project_value)
    for field, expected in _EXPECTED_PROJECT.items():
        _require(project.get(field) == expected, f"[project].{field} differs from the preserved setup contract")
    return project


def _assert_wheel_matches_project(wheel_path: Path, project: dict[str, Any]) -> None:
    """Compare wheel metadata with the declared PEP 621 values."""
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        payload = _wheel_payload(wheel_path)
        _require(_EXPECTED_WHEEL_MODULES <= payload.keys(), f"wheel is missing expected Ananta modules: {_EXPECTED_WHEEL_MODULES - payload.keys()}")
        entry_points_name = metadata_name.removesuffix("METADATA") + "entry_points.txt"
        metadata = email.parser.BytesParser().parsebytes(wheel.read(metadata_name))
        _require(metadata["Name"] == project["name"], "wheel name differs from [project].name")
        _require(metadata["Version"] == project["version"], "wheel version differs from [project].version")
        _require(
            metadata["Summary"] == project["description"],
            "wheel description differs from [project].description",
        )
        wheel_license = metadata.get("License-Expression") or metadata.get("License")
        _require(wheel_license == project["license"], "wheel license differs from [project].license")
        _require(
            metadata["Requires-Python"] == project["requires-python"],
            "wheel Python requirement differs from [project].requires-python",
        )
        _require(
            _normalized(metadata.get_all("Classifier", []))
            == _normalized(project.get("classifiers", [])),
            "wheel classifiers differ from [project].classifiers",
        )
        requirements = metadata.get_all("Requires-Dist", [])
        declared_requirements = list(project.get("dependencies", []))
        for extra, values in project.get("optional-dependencies", {}).items():
            declared_requirements.extend(f'{value}; extra == "{extra}"' for value in values)
        _require(
            _normalized(requirements) == _normalized(declared_requirements),
            f"wheel requirements differ from PEP 621 metadata: {requirements!r}",
        )
        _require(entry_points_name in wheel.namelist(), "wheel has no console entry points")
        parser = configparser.ConfigParser()
        parser.read_string(wheel.read(entry_points_name).decode("utf-8"))
        scripts = dict(parser.items("console_scripts")) if parser.has_section("console_scripts") else {}
        _require(
            scripts == project.get("scripts", {}),
            f"wheel console scripts differ from [project].scripts: {scripts!r}",
        )


def _wheel_payload(wheel_path: Path) -> dict[str, bytes]:
    """Read package and data members, leaving generated dist-info metadata out."""
    with zipfile.ZipFile(wheel_path) as wheel:
        return {
            name: wheel.read(name)
            for name in wheel.namelist()
            if not name.endswith("/") and ".dist-info/" not in name
        }


def _assert_payload_matches_legacy_setup(candidate: Path, legacy: Path) -> None:
    """Require PEP 621 builds to match legacy find_packages payload membership."""
    candidate_payload = _wheel_payload(candidate)
    legacy_payload = _wheel_payload(legacy)
    candidate_members = candidate_payload.keys()
    legacy_members = legacy_payload.keys()
    _require(
        candidate_members == legacy_members,
        "wheel package payload differs from legacy setup.py discovery: "
        f"candidate-only={sorted(candidate_members - legacy_members)!r}; "
        f"legacy-only={sorted(legacy_members - candidate_members)!r}",
    )
    mismatched_bytes = sorted(
        name for name in candidate_members if candidate_payload[name] != legacy_payload[name]
    )
    _require(not mismatched_bytes, f"wheel package bytes differ from legacy setup.py: {mismatched_bytes!r}")


def _write_legacy_setup(project_root: Path, project: dict[str, Any]) -> None:
    """Create a scratch baseline using the former find_packages setup behavior."""
    optional_dependencies = project["optional-dependencies"]
    scripts = [f"{name}={target}" for name, target in project["scripts"].items()]
    (project_root / "setup.py").write_text(
        "from setuptools import find_packages, setup\n\n"
        "setup(\n"
        f"    name={project['name']!r},\n"
        f"    version={project['version']!r},\n"
        f"    description={project['description']!r},\n"
        f"    license={project['license']!r},\n"
        '    package_dir={"": "src"},\n'
        '    packages=find_packages(where="src"),\n'
        "    include_package_data=True,\n"
        f"    classifiers={project['classifiers']!r},\n"
        f"    python_requires={project['requires-python']!r},\n"
        f"    install_requires={project['dependencies']!r},\n"
        f"    extras_require={optional_dependencies!r},\n"
        f'    entry_points={{"console_scripts": {scripts!r}}},\n'
        ")\n",
        encoding="utf-8",
    )
    (project_root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=77"]\n'
        'build-backend = "setuptools.build_meta"\n',
        encoding="utf-8",
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"metadata parity check: {message}")


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    """Run a packaging command and include captured output on failure."""
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"packaging command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _check_editable_install(
    project_root: Path, install_root: Path, env: dict[str, str], project: dict[str, Any]
) -> None:
    """Install editable and compare the resulting distribution metadata."""
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--editable",
            str(project_root),
            "--target",
            str(install_root),
        ],
        cwd=install_root.parent,
        env=env,
    )
    code = (
        "import importlib.metadata, importlib.util, json, site; "
        f"site.addsitedir({str(install_root)!r}); "
        "dist = next(dist for dist in importlib.metadata.distributions(path=["
        f"{str(install_root)!r}]) if dist.metadata['Name'].lower() == 'ananta'); "
        "spec = importlib.util.find_spec('ananta'); "
        "assert spec is not None and spec.origin is not None, 'editable Ananta module is absent'; "
        "print(json.dumps({'name': dist.metadata['Name'], 'version': dist.version, "
        "'requires': dist.requires or [], 'scripts': {entry.name: entry.value "
        "for entry in dist.entry_points if entry.group == 'console_scripts'}, "
        "'module_origin': spec.origin}))"
    )
    probe_env = {**env, "PYTHONNOUSERSITE": "1"}
    probe_env.pop("PYTHONPATH", None)
    probe_env.pop("PYTHONHOME", None)
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=install_root.parent,
        env=probe_env,
        capture_output=True,
        text=True,
        check=False,
    )
    _require(result.returncode == 0, f"editable metadata probe failed: {result.stderr}")
    _require('"name": "ananta"' in result.stdout, "editable install has the wrong name")
    installed = json.loads(result.stdout)
    _require(installed["version"] == project["version"], "editable install has the wrong version")
    module_origin = Path(installed["module_origin"]).resolve()
    editable_source = (project_root / "src").resolve()
    _require(
        module_origin.is_relative_to(editable_source),
        f"editable module origin {module_origin} is outside scratch source {editable_source}",
    )
    _require(
        installed["scripts"] == project.get("scripts", {}),
        "editable console scripts differ from [project].scripts",
    )
    requirements = list(project.get("dependencies", []))
    for extra, values in project.get("optional-dependencies", {}).items():
        requirements.extend(f'{value}; extra == "{extra}"' for value in values)
    _require(
        _normalized(installed["requires"]) == _normalized(requirements),
        "editable requirements differ from PEP 621 metadata",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=_DEFAULT_PROJECT_ROOT)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    project = _project_metadata(project_root)
    setup_source = (project_root / "setup.py").read_text(encoding="utf-8")
    _require(setup_source.strip() == "from setuptools import setup\n\nsetup()", "setup.py duplicates metadata")
    with tempfile.TemporaryDirectory(prefix="ananta-metadata-smoke-") as temporary_directory:
        temp_root = Path(temporary_directory)
        project_copy = temp_root / "ananta"
        shutil.copytree(project_root, project_copy)
        legacy_copy = temp_root / "legacy-ananta"
        shutil.copytree(project_root, legacy_copy)
        _write_legacy_setup(legacy_copy, project)
        wheel_dir = temp_root / "wheelhouse"
        wheel_dir.mkdir()
        legacy_wheel_dir = temp_root / "legacy-wheelhouse"
        legacy_wheel_dir.mkdir()
        env = os.environ.copy()
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        _run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(wheel_dir), str(project_copy)], cwd=temp_root, env=env)
        _run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(legacy_wheel_dir), str(legacy_copy)], cwd=temp_root, env=env)
        wheels = list(wheel_dir.glob("*.whl"))
        legacy_wheels = list(legacy_wheel_dir.glob("*.whl"))
        _require(len(wheels) == 1, f"expected one candidate wheel, got {wheels!r}")
        _require(len(legacy_wheels) == 1, f"expected one legacy baseline wheel, got {legacy_wheels!r}")
        _assert_wheel_matches_project(wheels[0], project)
        _assert_payload_matches_legacy_setup(wheels[0], legacy_wheels[0])
        _check_editable_install(project_copy, temp_root / "editable-target", env, project)
    print("PASS: Ananta PEP 621 metadata matches wheel and editable install metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
