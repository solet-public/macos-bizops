#!/usr/bin/env python3
"""Run repository Pyright with a validated, deterministic Node interpreter."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class NodeUnavailableError(RuntimeError):
    """The selected Node cannot execute; no type-checking verdict exists."""


class PyrightUnavailableError(RuntimeError):
    """The pinned Python environment has no usable installed Pyright JS."""


def installed_pyright_script() -> Path:
    """Find Pyright only in the invoking interpreter's installed package dirs."""
    locations = {Path(os.path.abspath(sysconfig.get_path(kind))) for kind in ("purelib", "platlib")}
    distributions = [(location, distribution) for location in locations for distribution in importlib.metadata.distributions(path=[str(location)]) if distribution.metadata.get("Name", "").casefold().replace("_", "-") == "pyright"]
    if not distributions:
        raise PyrightUnavailableError(f"Pyright package missing from invoking Python environment {sys.executable}")
    if len(distributions) != 1:
        raise PyrightUnavailableError(f"ambiguous installed Pyright distributions for {sys.executable}")
    location, distribution = distributions[0]
    script = Path(os.path.abspath(str(distribution.locate_file("pyright/dist/index.js"))))
    if not script.is_relative_to(location):
        raise PyrightUnavailableError(f"installed Pyright entry point outside the invoking interpreter's package location: {script}")
    if not script.is_file():
        raise PyrightUnavailableError(f"installed Pyright entry point missing: {script}")
    return script


def node_path(root: Path) -> Path:
    """Prefer repository Node, then the host's canonical prefix, then PATH.

    A selected but broken interpreter is an error, never silently skipped.
    macOS chooses its architecture's canonical prefix without consulting nvm.
    """
    local = root / ".venv/bin/node"
    if local.exists() or local.is_symlink():
        return local
    if platform.system() == "Darwin":
        prefix = "/opt/homebrew" if platform.machine() == "arm64" else "/usr/local"
        return Path(prefix) / "bin/node"
    found = shutil.which("node")
    if found is None:
        raise NodeUnavailableError("node interpreter missing from PATH")
    return Path(found)


def validated_node(root: Path) -> Path:
    node = node_path(root)
    try:
        probe = subprocess.run([str(node), "--eval", "console.log(process.arch); console.log(process.version)"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NodeUnavailableError(f"node interpreter {node} cannot execute: {exc}") from exc
    info = probe.stdout.strip().splitlines()
    expected_arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64", "i386": "ia32", "i686": "ia32"}.get(platform.machine())
    if probe.returncode or len(info) != 2 or not info[1].startswith("v"):
        raise NodeUnavailableError(f"node interpreter {node} failed version probe (exit {probe.returncode}): {probe.stderr.strip() or probe.stdout.strip()}")
    if expected_arch is None or info[0] != expected_arch:
        raise NodeUnavailableError(f"node interpreter {node} has architecture {info[0]}, expected {expected_arch} for {platform.machine()}")
    print(f"Node verified: {node} arch={info[0]} version={info[1]}", file=sys.stderr, flush=True)
    return node


_VALUE_OPTIONS = frozenset({"--createstub", "--level", "--pythonplatform", "--pythonpath", "--pythonversion", "--typeshedpath", "-t", "--venvpath", "-v", "--verifytypes"})
_JSON_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')


def takes_option_value(argv: list[str], index: int) -> bool:
    return argv[index] in _VALUE_OPTIONS or (argv[index] == "--threads" and index + 1 < len(argv) and argv[index + 1].isdigit())


def file_operands(argv: list[str]) -> tuple[list[str], list[str], str | None]:
    """Separate operands and project selection without consuming option values."""
    files: list[str] = []
    forwarded: list[str] = []
    project: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in {"-p", "--project"} and index + 1 < len(argv):
            project = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--project="):
            project = arg.split("=", 1)[1]
            index += 1
            continue
        forwarded.append(arg)
        if takes_option_value(argv, index) and index + 1 < len(argv):
            forwarded.append(argv[index + 1])
            index += 1
        elif not arg.startswith("-"):
            files.append(arg)
        index += 1
    return files, forwarded, project


def project_config(root: Path, project: str | None) -> Path:
    location = root if project is None else root / project
    if location.is_dir():
        json_config = location / "pyrightconfig.json"
        return json_config if json_config.is_file() else location / "pyproject.toml"
    return location


def read_config_settings(config: Path) -> dict[str, Any]:
    """Parse one Pyright config file's settings, from TOML or from JSON."""
    text = config.read_text(encoding="utf-8")
    if config.suffix == ".toml":
        parsed: dict[str, Any] = tomllib.loads(text).get("tool", {}).get("pyright", {})
        return parsed
    # Pyright accepts JSON with comments and trailing commas. Preserve strings
    # while removing those two syntactic extensions, including URL strings.
    uncommented = _JSON_TOKENS.sub(lambda match: " " if match[0].startswith("/") else match[0], text)
    clean = re.sub(r'("(?:\\.|[^"\\])*")|,\s*(?=[}\]])', lambda match: match[1] or "", uncommented)
    loaded: dict[str, Any] = json.loads(clean)
    return loaded


def effective_config(config: Path, seen: frozenset[Path] = frozenset()) -> tuple[Path, dict[str, Any]]:
    """The config that supplies ``exclude``, and its settings.

    Walks ``extends`` exactly as ``config_exclusions`` always has: the first
    config in the chain that states ``exclude`` is the effective one. Its
    directory is the base every relative path setting in it resolves against.
    """
    config = config.resolve()
    if config in seen:
        return config, {}  # Pyright owns diagnostics for invalid extends chains.
    settings = read_config_settings(config)
    if "exclude" in settings:
        return config, settings
    if "extends" in settings:
        return effective_config((config.parent / settings["extends"]).absolute(), seen | {config})
    return config, {}


def config_exclusions(config: Path, seen: frozenset[Path] = frozenset()) -> tuple[Path, list[str]]:
    """Read effective excludes; let Pyright inherit every other setting itself."""
    effective, settings = effective_config(config, seen)
    return effective.parent, list(settings.get("exclude", []))


#: Path settings Pyright accepts ABSOLUTE. An overlay config lives outside the
#: repository, so Pyright would otherwise resolve these against the overlay's
#: own directory and find nothing -- no venv, no extra search paths -- making
#: every import unresolvable. ``venv`` is deliberately absent: it is a directory
#: NAME looked up under ``venvPath``, not a path.
_ABSOLUTE_SETTINGS: tuple[str, ...] = ("venvPath", "extraPaths", "typeshedPath", "stubPath")

#: Path settings Pyright requires RELATIVE to the config stating them: it warns
#: `Ignoring path "..." because it is not relative` and DROPS an absolute entry,
#: which would silently narrow the analyzed set. These are rewritten relative to
#: the overlay instead, the same way ``exclude`` already is.
_RELATIVE_SETTINGS: tuple[str, ...] = ("include", "ignore", "strict")


def overlay_payload(config: Path, effective: Path, settings: Mapping[str, Any], overlay_directory: Path, retained: list[str]) -> dict[str, Any]:
    """The complete overlay config for a selected-test run.

    Extracted so the regression smoke measures THIS function rather than
    re-deriving the payload beside it: a test that rebuilds the payload itself
    can agree with a broken production path.
    """
    origin = effective.parent
    relative_origin = Path(os.path.relpath(origin, overlay_directory))
    inherited = overlay_path_settings(origin, overlay_directory, settings)
    return {
        "extends": str(config.absolute()),
        **inherited,
        **execution_environments(origin, settings, inherited),
        "exclude": [str(relative_origin / pattern) for pattern in retained],
    }


def execution_environments(origin: Path, settings: Mapping[str, Any], inherited: Mapping[str, Any]) -> dict[str, Any]:
    """Name the repository as Pyright's import-resolution root for the overlay.

    Pyright resolves imports from the EXECUTION ROOT, which defaults to the
    directory holding the config. An overlay config lives outside the
    repository, so that root moves with it and every repository-local import
    stops resolving -- a top-level package such as ``quality_gates``, and also
    the own-directory sibling imports that modules like ``run_smokes`` and
    ``code_quality_check`` use (``from gate_scope import ...``, resolved at
    runtime by each module inserting its own directory on ``sys.path``). Stating
    the real root restores exactly what the overlay displaced and nothing more.

    A config that already declares ``executionEnvironments`` keeps its own: its
    roots are rebased absolute and nothing is invented on top of them.
    """
    declared = settings.get("executionEnvironments")
    if isinstance(declared, list):
        rebased: list[Any] = []
        for environment in declared:
            root = environment.get("root") if isinstance(environment, dict) else None
            rebased.append({**environment, "root": str(origin / root)} if isinstance(root, str) else environment)
        return {"executionEnvironments": rebased}
    if "executionEnvironments" in settings:
        return {}  # Pyright owns the diagnostic for a malformed value.
    environment: dict[str, Any] = {"root": str(origin)}
    if "extraPaths" in inherited:
        environment["extraPaths"] = inherited["extraPaths"]
    return {"executionEnvironments": [environment]}


def _rewrite(value: Any, rebase: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return rebase(value)
    if isinstance(value, list):
        return [rebase(entry) if isinstance(entry, str) else entry for entry in value]
    return value


def overlay_path_settings(origin: Path, overlay_directory: Path, settings: Mapping[str, Any]) -> dict[str, Any]:
    """Re-express ``settings``' path values for a config at ``overlay_directory``.

    Only keys actually present are emitted, so the overlay states nothing the
    real config did not state and Pyright keeps inheriting everything else.
    """
    relative_origin = Path(os.path.relpath(origin, overlay_directory))
    rewritten: dict[str, Any] = {}
    for key in _ABSOLUTE_SETTINGS:
        if key in settings:
            rewritten[key] = _rewrite(settings[key], lambda entry: str(origin / entry))
    for key in _RELATIVE_SETTINGS:
        if key in settings:
            rewritten[key] = _rewrite(settings[key], lambda entry: str(relative_origin / entry))
    return rewritten


def suppresses_test(pattern: str, origin: Path, selected: list[Path]) -> bool:
    """Lift matching test-directory specs only, retaining all other exemptions."""
    parts = Path(pattern).parts
    if not (parts[-1:] == ("tests",) or parts[-2:] == ("tests", "**")):
        return False
    absolute = os.path.normpath(origin / pattern)
    return any(parent.full_match(absolute) for path in selected for parent in (path, *path.parents))


@contextmanager
def selected_config(root: Path, argv: list[str]) -> Generator[list[str]]:
    files, forwarded, project = file_operands(argv)
    selected = [Path(os.path.abspath(root / path)) for path in files if Path(path).suffix == ".py" and "tests" in Path(path).parts]
    if not selected:
        yield argv
        return
    config = project_config(root, project)
    effective, settings = effective_config(config)
    origin = effective.parent
    excludes = list(settings.get("exclude", []))
    retained = [pattern for pattern in excludes if not suppresses_test(pattern, origin, selected)]
    if retained == excludes:
        yield argv
        return
    temporary_parent = Path(tempfile.gettempdir()).resolve()
    if temporary_parent.is_relative_to(root):
        temporary_parent = root.parent
    with tempfile.TemporaryDirectory(prefix="pyright-selected-tests-", dir=temporary_parent) as directory:
        overlay = Path(directory) / "pyrightconfig.json"
        # The overlay deliberately lives OUTSIDE the repository, so every path
        # setting it inherits has to be stated absolutely: Pyright resolves a
        # relative setting against the config that states it, which is now this
        # temporary directory. Without this the repo's `venvPath = "."` and its
        # relative `extraPaths`/`include` would point at an empty temp directory
        # and every import would be reported unresolved -- a refusal nobody can
        # pass (rul_becb4bed), not a typing verdict.
        overlay.write_text(json.dumps(overlay_payload(config, effective, settings, overlay.parent, retained)), encoding="utf-8")
        print(f"Pyright selected-test config: {overlay}", file=sys.stderr, flush=True)
        yield [*forwarded, "--project", str(overlay)]


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    try:
        node = validated_node(root)
    except NodeUnavailableError as exc:
        print(f"FATAL: Pyright never ran: {exc}", file=sys.stderr)
        return 70
    # Execute the already-installed JS directly: pyright's Python launcher can
    # install nodeenv or Pyright on demand. No installer is ever called here.
    try:
        script = installed_pyright_script()
    except PyrightUnavailableError as exc:
        print(f"FATAL: Pyright never ran: {exc}", file=sys.stderr)
        return 70
    print(f"Pyright node interpreter: {node}", file=sys.stderr, flush=True)
    try:
        with selected_config(root, sys.argv[1:] if argv is None else argv) as arguments:
            return subprocess.run([str(node), str(script), *arguments], cwd=root, check=False).returncode
    except OSError as exc:
        print(f"FATAL: Pyright never ran with node {node}: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
