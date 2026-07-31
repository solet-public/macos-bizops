"""No-MCP-first launcher smoke — the per-homunculus PATH command birth step.

Drives `install_command_launcher_at_birth()` against tmpfs clone + bin dirs
(no real `~/.local/bin`, no venv). Asserts the full contract:

* happy path installs `<bin_dir>/<name>` as a symlink to the clone's own
  `homunculus` console script,
* re-run is idempotent (`already_installed`, symlink untouched),
* a stale symlink (pointing elsewhere) is repointed,
* a NON-symlink file at the launcher path is a fail-loud refusal (never
  clobber an operator file),
* a missing console script is a fail-loud refusal (venv must be provisioned
  first),
* an invalid homunculus name is refused (defense in depth: `bin_dir / name`
  must never escape bin_dir).

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/command_launcher_smoke.py``.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from github_midwife_plugin.command_launcher import (  # noqa: E402
    CONSOLE_SCRIPT_NAME,
    CommandLauncherError,
    install_command_launcher_at_birth,
)

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _make_clone(root: Path, name: str = "clone") -> Path:
    clone = root / name
    (clone / ".venv" / "bin").mkdir(parents=True)
    (clone / ".venv" / "bin" / CONSOLE_SCRIPT_NAME).write_text("#!/bin/sh\n")
    return clone


def _check_install_idempotent_repoint(root: Path) -> None:
    clone = _make_clone(root)
    bin_dir = root / "bin"
    target = clone / ".venv" / "bin" / CONSOLE_SCRIPT_NAME

    installed = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=bin_dir,
    )
    launcher = bin_dir / "testhum"
    _check(
        "fresh install creates the symlink and reports installed",
        installed.status == "installed"
        and launcher.is_symlink()
        and launcher.readlink() == target,
        f"{installed} link={launcher}",
    )

    again = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=bin_dir,
    )
    _check(
        "re-run over a correct launcher is an idempotent no-op",
        again.status == "already_installed" and launcher.readlink() == target,
        str(again),
    )

    other_clone = _make_clone(root, name="other_clone")
    launcher.unlink()
    launcher.symlink_to(other_clone / ".venv" / "bin" / CONSOLE_SCRIPT_NAME)
    repointed = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=bin_dir,
    )
    _check(
        "a stale symlink (another clone's script) is repointed to this clone",
        repointed.status == "repointed" and launcher.readlink() == target,
        f"{repointed} -> {launcher.readlink()}",
    )


def _check_failure_modes(root: Path) -> None:
    clone = _make_clone(root, name="failure_clone")
    bin_dir = root / "failure_bin"
    bin_dir.mkdir()

    (bin_dir / "occupied").write_text("an operator's real file\n")
    try:
        install_command_launcher_at_birth(
            name="occupied", clone_root=clone, bin_dir=bin_dir,
        )
        raise SmokeFailureError("non-symlink collision did not raise")
    except CommandLauncherError as exc:
        _check(
            "a NON-symlink at the launcher path is a fail-loud refusal",
            "refusing to clobber" in str(exc),
            str(exc),
        )
    _check(
        "the operator's file survives the refusal untouched",
        (bin_dir / "occupied").read_text() == "an operator's real file\n",
    )

    bare_clone = root / "bare_clone"
    (bare_clone / ".venv" / "bin").mkdir(parents=True)
    try:
        install_command_launcher_at_birth(
            name="testhum", clone_root=bare_clone, bin_dir=bin_dir,
        )
        raise SmokeFailureError("missing console script did not raise")
    except CommandLauncherError as exc:
        _check(
            "a missing console script is a fail-loud refusal",
            "console script missing" in str(exc),
            str(exc),
        )

    try:
        install_command_launcher_at_birth(
            name="../escape", clone_root=clone, bin_dir=bin_dir,
        )
        raise SmokeFailureError("invalid name did not raise")
    except CommandLauncherError as exc:
        _check(
            "an invalid homunculus name is refused before touching bin_dir",
            "invalid homunculus name" in str(exc),
            str(exc),
        )
    _check(
        "the refused name created nothing outside bin_dir",
        not (root / "escape").exists(),
    )


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_install_idempotent_repoint(root)
            _check_failure_modes(root)
    except SmokeFailureError as exc:
        print(f"command_launcher_smoke FAILED: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"command_launcher_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
