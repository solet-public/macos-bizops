"""Fail-closed Homebrew package mutation boundary for bootstrap routes."""

from __future__ import annotations

import os
import pwd
import re
import subprocess

from .models import INSTALL_TIMEOUT_SECONDS, AdapterError, AdapterRuntime

_HOMEBREW_GUARD_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
    "HOMEBREW_NO_INSTALL_UPGRADE": "1",
}
_OUTPUT_LIMIT = 16_384
_PACKAGE_ITEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+/-]*$")
_PACKAGE_HEADER = re.compile(
    r"^Would install (?P<count>[1-9][0-9]*) (?P<kind>cask|casks|formula|formulas|formulae):$"
)
_DEPENDENCY_HEADER = re.compile(
    r"^Would install (?P<count>[1-9][0-9]*) dependenc(?:y|ies) for (?P<parent>.+):$"
)


class HomebrewInstallError(AdapterError):
    """A Homebrew failure that retains the bounded command streams."""

    def __init__(self, message: str, *, stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.stdout = stdout[:_OUTPUT_LIMIT]
        self.stderr = stderr[:_OUTPUT_LIMIT]


def homebrew_guard_environment() -> dict[str, str]:
    """Preserve Homebrew policy and bind the guard to the target account home."""

    environment = {key: value for key, value in os.environ.items() if key.startswith("HOMEBREW_")}
    environment.update(_HOMEBREW_GUARD_ENV)
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError as exc:
        raise AdapterError("Homebrew guard could not resolve the target account home") from exc
    if not home or not os.path.isabs(home):
        raise AdapterError("Homebrew guard resolved an invalid target account home")
    environment["HOME"] = home
    return environment


def _normalized_plan_lines(output: str) -> list[str]:
    """Remove Homebrew's informational prefix before interpreting plan headings."""

    return [line.strip().removeprefix("==> ").strip() for line in output.splitlines()]


def _plan_block_header(
    line: str, *, kind: str
) -> tuple[list[str], int, str | None] | None:
    """Return a recognized package or dependency heading's empty item block."""

    package_header = _PACKAGE_HEADER.fullmatch(line)
    if package_header is not None:
        header_kind = package_header["kind"].rstrip("s")
        if header_kind == "formulae":
            header_kind = "formula"
        if header_kind != kind:
            return None
        return [], int(package_header["count"]), None
    dependency_header = _DEPENDENCY_HEADER.fullmatch(line)
    if dependency_header is None:
        return None
    return [], int(dependency_header["count"]), dependency_header["parent"]


def _homebrew_install_items(
    lines: list[str], *, kind: str
) -> tuple[list[list[str]], list[tuple[str, list[str]]]] | None:
    """Extract requested-package blocks and their declared dependency blocks."""

    package_blocks: list[list[str]] = []
    dependency_blocks: list[tuple[str, list[str]]] = []
    block_counts: list[tuple[list[str], int]] = []
    current_items: list[str] | None = None
    for line in lines:
        if line.startswith("Would install"):
            block = _plan_block_header(line, kind=kind)
            if block is None:
                return None
            current_items, declared_count, parent = block
            block_counts.append((current_items, declared_count))
            if parent is None:
                package_blocks.append(current_items)
            else:
                dependency_blocks.append((parent, current_items))
            continue
        if current_items is not None and _PACKAGE_ITEM.fullmatch(line) is not None:
            current_items.append(line)
    if any(len(items) != declared_count for items, declared_count in block_counts):
        return None
    return package_blocks, dependency_blocks


def _dependency_closure_is_exact(
    package: str, dependency_blocks: list[tuple[str, list[str]]]
) -> bool:
    """Require every declared dependency block to be reachable from the request."""

    approved = {package}
    pending = list(dependency_blocks)
    while pending:
        unresolved = [(parent, items) for parent, items in pending if parent not in approved]
        for parent, dependencies in pending:
            if parent in approved:
                approved.update(dependencies)
        if len(unresolved) == len(pending):
            return False
        pending = unresolved
    return True


def _homebrew_plan_is_exact(output: str, *, kind: str, package: str) -> bool:
    lines = _normalized_plan_lines(output)
    prohibited = ("Would upgrade", "Would reinstall", "Would remove", "Would unlink")
    if kind not in {"cask", "formula"} or any(line.startswith(prohibited) for line in lines):
        return False
    parsed = _homebrew_install_items(lines, kind=kind)
    if parsed is None:
        return False
    package_blocks, dependency_blocks = parsed
    if not package_blocks or any(items != [package] for items in package_blocks):
        return False
    return _dependency_closure_is_exact(package, dependency_blocks)


def run_homebrew_install_required(
    runtime: AdapterRuntime,
    brew: str,
    package: str,
    label: str,
    *,
    kind: str = "formula",
) -> None:
    """Fail before apply when Homebrew proposes collateral package mutation."""

    if kind not in {"cask", "formula"}:
        raise AdapterError(f"{label} has an unreviewed Homebrew package kind")
    package_args = ("--cask", package) if kind == "cask" else (package,)
    environment = homebrew_guard_environment()
    try:
        dry_run = runtime.run(
            [brew, "install", "--dry-run", *package_args],
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"{label} dry-run could not execute") from exc
    if dry_run.returncode != 0 or not _homebrew_plan_is_exact(
        f"{dry_run.stdout}\n{dry_run.stderr}", kind=kind, package=package
    ):
        raise HomebrewInstallError(
            f"{label} dry-run proposed an unapproved package mutation",
            stdout=dry_run.stdout,
            stderr=dry_run.stderr,
        )
    try:
        completed = runtime.run(
            [brew, "install", *package_args],
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"{label} could not execute") from exc
    if completed.returncode != 0:
        raise HomebrewInstallError(
            f"{label} failed (exit {completed.returncode})",
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
