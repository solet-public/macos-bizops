"""Hermetic receipt fixtures for the bootstrap Homebrew mutation guard."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

# ruff: noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bootstrap_adapter.homebrew import _homebrew_plan_is_exact
from bootstrap_adapter.models import AdapterRuntime, PostgresObservation
from bootstrap_adapter.routes import _coding_tool_route, _postgres_install_route

_RECEIPT_049_CODEX = "==> Would install 1 cask:\ncodex\n"
_RECEIPT_054_POSTGRES_STDOUT = """codex-cli 0.153.4
2.1.236 (Claude Code)
==> Would install 1 formula:
postgresql@17
==> Downloading https://ghcr.io/v2/homebrew/core/postgresql/17/manifests/17.11
Already downloaded: /Users/admin/Library/Caches/Homebrew/downloads/e4ff9c3d52f936d2bd96532d8a86426abfd82dc11b1176d5feefe727ffb449e8--postgresql@17-17.11.bottle_manifest.json
==> Would install 1 dependency for postgresql@17:
krb5
==> Would install 1 formula:
postgresql@17
==> Would install 1 dependency for postgresql@17:
krb5
"""
_RECEIPT_054_POSTGRES_STDERR = """Warning: `$HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK` is set: not checking for outdated
dependents or dependents with broken linkage!
"""


def _check(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def _request(operation_id: str) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "phase": "apply",
        "probe_purpose": None,
        "request_id": "fixture",
        "name": "fixture",
    }


def _runtime(stdout: str, stderr: str, returncode: int) -> AdapterRuntime:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "Homebrew fixture\n", "")
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )


def _apply_failure_runtime(stdout: str, stderr: str) -> AdapterRuntime:
    """Return an exact dry-run plan followed by a failed mutating invocation."""

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "Homebrew fixture\n", "")
        if "--dry-run" in command:
            return subprocess.CompletedProcess(command, 0, _RECEIPT_049_CODEX, "")
        return subprocess.CompletedProcess(command, 1, stdout, stderr)

    return AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )


def _check_failure_envelopes() -> None:
    stdout = "Homebrew stdout marker\n"
    stderr = "Homebrew stderr marker\n"
    failed_coding_tool = _coding_tool_route(
        _request("install_codex_cli"), _runtime(stdout, stderr, 1)
    )
    _check(
        failed_coding_tool["stdout"] == stdout and failed_coding_tool["stderr"] == stderr,
        "coding-tool failure preserves exact captured Homebrew streams",
    )

    failed_apply = _coding_tool_route(
        _request("install_codex_cli"), _apply_failure_runtime(stdout, stderr)
    )
    _check(
        failed_apply["stdout"] == stdout and failed_apply["stderr"] == stderr,
        "coding-tool apply failure preserves exact captured Homebrew streams",
    )

    observation = PostgresObservation(True, "/fixture/brew", False, False, None, False, None)
    with patch("bootstrap_adapter.routes.postgres_observation", return_value=observation):
        failed_postgres = _postgres_install_route(
            _request("install_postgresql"), _runtime(stdout, stderr, 1)
        )
    _check(
        failed_postgres["stdout"] == stdout and failed_postgres["stderr"] == stderr,
        "PostgreSQL Homebrew failure preserves exact captured streams",
    )

    oversized = "x" * 16_385
    bounded = _coding_tool_route(_request("install_codex_cli"), _runtime(oversized, "", 1))
    _check(
        bounded["stdout"] == oversized[:16_384],
        "Homebrew failure stdout is bounded at the envelope schema limit",
    )


def _check_fresh_postgres_installs_pgvector_after_service_start() -> None:
    """A fresh host can discover pgvector only after PostgreSQL starts."""

    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "--dry-run" in command:
            package = command[-1]
            return subprocess.CompletedProcess(
                command,
                0,
                f"==> Would install 1 formula:\n{package}\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    runtime = AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )
    fresh = PostgresObservation(True, "/fixture/brew", False, False, None, False, None)
    started_without_pgvector = PostgresObservation(
        True, "/fixture/brew", True, True, 17, True, False
    )
    with patch(
        "bootstrap_adapter.routes.postgres_observation",
        side_effect=[fresh, started_without_pgvector],
    ) as observe:
        applied = _postgres_install_route(_request("install_postgresql"), runtime)

    _check(
        applied["checkpoint_status"] == "applied",
        "fresh PostgreSQL install applies successfully",
    )
    _check(
        observe.call_count == 2,
        "fresh PostgreSQL install re-observes after starting the service",
    )
    _check(
        commands
        == [
            ["/fixture/brew", "install", "--dry-run", "postgresql@17"],
            ["/fixture/brew", "install", "postgresql@17"],
            ["/fixture/brew", "services", "start", "postgresql@17"],
            ["/fixture/brew", "install", "--dry-run", "pgvector"],
            ["/fixture/brew", "install", "pgvector"],
        ],
        "fresh PostgreSQL install adds pgvector only after the post-start observation reports it absent",
    )


def main() -> int:
    _check(
        _homebrew_plan_is_exact(_RECEIPT_049_CODEX, kind="cask", package="codex"),
        "receipt-049 arrow-prefixed Codex cask plan is accepted",
    )
    _check(
        _homebrew_plan_is_exact(
            _RECEIPT_054_POSTGRES_STDOUT + _RECEIPT_054_POSTGRES_STDERR,
            kind="formula",
            package="postgresql@17",
        ),
        "receipt-054 repeated PostgreSQL dependency closure and warning are accepted",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 2 formulae:\npostgresql@17\nunrelated\n",
            kind="formula",
            package="postgresql@17",
        ),
        "two unrelated top-level packages remain refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 1 formula:\npostgresql@17\n==> Would upgrade 1 formula:\npython@3.13\n",
            kind="formula",
            package="postgresql@17",
        ),
        "a hidden upgrade remains refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 1 formula:\npostgresql@17\n"
            "==> Would install 1 dependency for unrelated:\nkrb5\n",
            kind="formula",
            package="postgresql@17",
        ),
        "an unrelated dependency-block parent remains refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 1 formula:\npostgresql@17\n"
            "==> Would install 1 dependency for postgresql@17:\nkrb5\ngssapi\n",
            kind="formula",
            package="postgresql@17",
        ),
        "a dependency block with more items than its declared count remains refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            "Warning: Homebrew produced no install plan\n",
            kind="formula",
            package="postgresql@17",
        ),
        "output without a package-plan heading remains refused",
    )
    _check_failure_envelopes()
    _check_fresh_postgres_installs_pgvector_after_service_start()
    print("bootstrap_adapter_homebrew_plan_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
