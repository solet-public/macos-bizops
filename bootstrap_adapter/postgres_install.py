"""Approved PostgreSQL install actions and fresh service-readiness confirmation."""

from __future__ import annotations

import hashlib
import os
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .homebrew import CommandExecutionError, CommandOutcome, run_homebrew_install_required
from .models import INSTALL_TIMEOUT_SECONDS, SUPPORTED_POSTGRES_MAJOR, AdapterRuntime, PostgresObservation
from .postgres import postgres_observation, run_required, wait_for_postgres_ready

# Homebrew's pgvector formula stopped building for PostgreSQL 17 on 2026-11-19, so the seed builds the pgvector it needs.
# This is the one place that names the release: a pgvector security fix is a change to this line, and the sha256 equals
# Homebrew's own pin for the same tag.
PGVECTOR_VERSION, PGVECTOR_SHA256 = "0.8.7", "cac0b10c360f05b2d521200105ba3697e773d4cd3731f5a915a7e37ebe0bea85"
PGVECTOR_URL = f"https://github.com/pgvector/pgvector/archive/refs/tags/v{PGVECTOR_VERSION}.tar.gz"

_POSTGRES_FORMULA = f"postgresql@{SUPPORTED_POSTGRES_MAJOR}"
_CURL = "/usr/bin/curl"
_XCRUN = "/usr/bin/xcrun"
_MAKE = "/usr/bin/make"
_STDERR_LIMIT = 16_384  # the setup envelope's stderr cap
_COMMAND_LINE_TOOLS_REPAIR = "Install the Xcode Command Line Tools with `xcode-select --install`, wait for the installer to finish, then resume setup."


@dataclass(frozen=True)
class PostgresServiceStart:
    """The command receipt and fresh state observed after a service start attempt."""

    outcome: CommandOutcome
    ready: bool
    observation: PostgresObservation


def _start_service(runtime: AdapterRuntime, brew: str) -> PostgresServiceStart:
    """Start the service, then prove readiness with a fresh full observation."""

    try:
        outcome = run_required(runtime, [brew, "services", "start", "postgresql@17"], "PostgreSQL service start")
    except CommandExecutionError as exc:
        outcome = exc.outcome
    polled_ready = wait_for_postgres_ready(runtime)
    observation = postgres_observation(runtime)
    return PostgresServiceStart(outcome, polled_ready and observation.ready, observation)


def _refusal(message: str, *, returncode: int | None = None) -> CommandExecutionError:
    """Refuse with a reason that reaches the adopter.

    The install route reports a CommandExecutionError's outcome and never the text of a bare AdapterError, which would
    arrive as the route's generic 'Inspect the Homebrew package state'. Layer 0 reports `str(error)`.
    """

    return CommandExecutionError(message, outcome=CommandOutcome(returncode, False, 0, "", message[:_STDERR_LIMIT]))


def _curl_command(output: str) -> list[str]:
    return [_CURL, "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--tlsv1.2", "--max-time", str(INSTALL_TIMEOUT_SECONDS), "--output", output, PGVECTOR_URL]


def _make_command(source: str, pg_config: str, sdk: str, *targets: str) -> list[str]:
    """OPTFLAGS= is the portable build; PG_SYSROOT replaces the SDK path baked into the Homebrew bottle's pg_config."""

    return [_MAKE, "-C", source, f"PG_CONFIG={pg_config}", "OPTFLAGS=", f"PG_SYSROOT={sdk}", *targets]


def pgvector_build_commands() -> tuple[str, ...]:
    """The commands install_pgvector_from_source runs, with placeholders for what only the host knows, for a confirm message."""

    source = f"<temp dir>/source/pgvector-{PGVECTOR_VERSION}"
    commands = (
        _curl_command("<temp dir>/pgvector.tar.gz"),
        _make_command(source, f"<{_POSTGRES_FORMULA} pg_config>", "<macOS SDK path>"),
        _make_command(source, f"<{_POSTGRES_FORMULA} pg_config>", "<macOS SDK path>", "install"),
    )
    return tuple(f"$ {' '.join(command)}" for command in commands)


def _command_line_tools_sdk(runtime: AdapterRuntime) -> str:
    """Return the macOS SDK path, or refuse naming the repair before anything is downloaded."""

    sdk = ""
    for command in ([_XCRUN, "--find", "clang"], [_XCRUN, "--find", "make"], [_XCRUN, "--sdk", "macosx", "--show-sdk-path"]):
        try:
            sdk = run_required(runtime, command, f"Command Line Tools check ({' '.join(command)})").stdout.strip()
        except CommandExecutionError as exc:
            raise _refusal(f"{exc} {_COMMAND_LINE_TOOLS_REPAIR}", returncode=exc.outcome.returncode) from exc
    if not sdk or not Path(sdk).is_dir():
        raise _refusal(f"xcrun reported no macOS SDK directory ({sdk!r}). {_COMMAND_LINE_TOOLS_REPAIR}")
    return sdk


def _pinned_pg_config(runtime: AdapterRuntime, brew: str) -> Path:
    """The pg_config of the PostgreSQL major the seed pins, not whichever one is first on PATH."""

    prefix = run_required(runtime, [brew, "--prefix", _POSTGRES_FORMULA], f"{_POSTGRES_FORMULA} prefix lookup").stdout.strip()
    pg_config = Path(prefix) / "bin" / "pg_config"
    if not prefix or not pg_config.is_file() or not os.access(pg_config, os.X_OK):
        raise _refusal(f"{_POSTGRES_FORMULA} has no executable pg_config at {pg_config}; repair it with `brew reinstall {_POSTGRES_FORMULA}` and resume setup.")
    return pg_config


def _fetch_pinned_source(runtime: AdapterRuntime, scratch: Path) -> Path:
    """Download the pinned tarball, check its sha256 BEFORE opening it, and extract it without leaving the directory."""

    archive = scratch / "pgvector.tar.gz"
    run_required(runtime, _curl_command(str(archive)), "pgvector source download")
    try:
        measured = hashlib.sha256(archive.read_bytes()).hexdigest()
    except OSError as exc:
        raise _refusal(f"pgvector source download left no readable archive: {exc}") from exc
    if measured != PGVECTOR_SHA256:
        raise _refusal(f"pgvector {PGVECTOR_VERSION} download is not the pinned bytes (expected sha256 {PGVECTOR_SHA256}, measured {measured}); nothing was built.")
    source = scratch / "source"
    try:
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise _refusal(f"pgvector source archive could not be extracted safely: {exc}") from exc
    return source / f"pgvector-{PGVECTOR_VERSION}"


def _require_installed_module(runtime: AdapterRuntime, pg_config: Path) -> None:
    """A `make install` that exits 0 is not the module: an install that exits 0 and installs nothing is the defect this replaces."""

    pkglibdir = Path(run_required(runtime, [str(pg_config), "--pkglibdir"], "pg_config --pkglibdir").stdout.strip())
    sharedir = Path(run_required(runtime, [str(pg_config), "--sharedir"], "pg_config --sharedir").stdout.strip())
    absent = [path for path in (pkglibdir / "vector.dylib", sharedir / "extension" / "vector.control") if not path.is_file()]
    if absent:
        raise _refusal(f"pgvector make install exited 0 but the module is absent after install: {', '.join(str(path) for path in absent)}")


def install_pgvector_from_source(runtime: AdapterRuntime, brew: str) -> None:
    """Build the pinned pgvector against the pinned PostgreSQL major, install it beside that PostgreSQL, and prove the module is there.

    Homebrew's pgvector no longer carries a PostgreSQL 17 module, so `brew install pgvector` exits 0 and installs none.
    Callers probe first and call this only when pgvector is not already available.
    """

    sdk = _command_line_tools_sdk(runtime)
    pg_config = _pinned_pg_config(runtime, brew)
    with tempfile.TemporaryDirectory(prefix="solet-pgvector-") as scratch:
        source = str(_fetch_pinned_source(runtime, Path(scratch)))
        run_required(runtime, _make_command(source, str(pg_config), sdk), "pgvector build")
        run_required(runtime, _make_command(source, str(pg_config), sdk, "install"), "pgvector install")
    _require_installed_module(runtime, pg_config)


@dataclass(frozen=True)
class PostgresInstallApply:
    """The failed service start, if any, and the dependencies Homebrew upgraded on the way."""

    failed_start: PostgresServiceStart | None
    upgraded_dependencies: tuple[str, ...]


def apply_postgres_install_actions(
    runtime: AdapterRuntime,
    brew: str,
    actions: Sequence[dict[str, Any]],
) -> PostgresInstallApply:
    """Apply the approved package/service plan without retaining stale readiness."""

    packages = {"postgres.install_homebrew_formula": ("postgresql@17", "PostgreSQL install")}
    refreshed: PostgresObservation | None = None
    upgraded: list[str] = []
    for item in actions:
        action_id = str(item["id"])
        if action_id == "postgres.start_homebrew_service":
            transition = _start_service(runtime, brew)
            refreshed = transition.observation
            if not transition.ready:
                return PostgresInstallApply(transition, tuple(upgraded))
            continue
        if action_id == "postgres.install_pgvector_formula":
            if refreshed is None or refreshed.pgvector_available is not True:
                install_pgvector_from_source(runtime, brew)
            continue
        package, label = packages[action_id]
        upgraded.extend(run_homebrew_install_required(runtime, brew, package, label).upgraded_dependencies)
    return PostgresInstallApply(None, tuple(dict.fromkeys(upgraded)))
