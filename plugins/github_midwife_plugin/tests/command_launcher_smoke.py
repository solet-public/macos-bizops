"""No-MCP-first launcher smoke — the per-solet PATH command birth step.

Drives `install_command_launcher_at_birth()` against tmpfs clone + bin dirs
(no real `~/.local/bin`, no venv). Asserts the full contract:

* happy path installs `<bin_dir>/<name>` as a symlink to the clone's own
  `solet` console script,
* an existing same-name Codex MCP table is a fail-loud refusal,
* a stale symlink (pointing elsewhere) is repointed,
* a NON-symlink file at the launcher path is a fail-loud refusal (never
  clobber an operator file),
* a missing console script is a fail-loud refusal (venv must be provisioned
  first),
* an invalid solet name is refused (defense in depth: `bin_dir / name`
  must never escape bin_dir).

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/command_launcher_smoke.py``.
"""

from __future__ import annotations

import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from github_midwife_plugin.command_launcher import (  # noqa: E402
    CONSOLE_SCRIPT_NAME,
    CommandLauncherError,
    _mcp_block,
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


def _check_fresh_install(root: Path) -> tuple[Path, Path, Path, Path]:
    clone = _make_clone(root)
    bin_dir = root / "bin"
    config_path = root / "codex" / "config.toml"
    target = clone / ".venv" / "bin" / CONSOLE_SCRIPT_NAME

    installed = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=bin_dir, codex_config_path=config_path,
    )
    launcher = bin_dir / "testhum"
    _check(
        "fresh install creates the symlink and reports installed",
        installed.status == "installed"
        and launcher.is_symlink()
        and launcher.readlink() == target,
        f"{installed} link={launcher}",
    )
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    server = parsed.get("mcp_servers", {}).get("testhum", {})
    _check(
        server.get("command") == str(clone / ".venv" / "bin" / "python3")
        and server.get("args") == ["-m", "agent_messaging_plugin.mcp_bridge"]
        and server.get("env_vars") == ["CODEX_THREAD_ID"]
        and server.get("env", {}).get("SOLET_NAME") == "testhum"
        and server.get("env", {}).get("AGENT_IDENTITY") == "codex"
        and server.get("env", {}).get("AGENT_SESSION_LABEL") == "Codex-Ambient",
        "fresh install appends the newborn's own Codex MCP bridge configuration",
        repr(server),
    )

    return clone, bin_dir, config_path, target


def _check_self_generated_config_is_idempotent(
    clone: Path,
    bin_dir: Path,
    config_path: Path,
    target: Path,
) -> None:
    generated = config_path.read_text(encoding="utf-8")
    duplicate = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=bin_dir, codex_config_path=config_path,
    )
    _check(
        "an exact self-generated Codex MCP table is idempotent rather than refused",
        duplicate.status == "already_installed"
        and duplicate.mcp_status == "already_installed"
        and "already installed" in duplicate.mcp_reason
        and (bin_dir / "testhum").readlink() == target
        and config_path.read_text(encoding="utf-8") == generated,
        repr(duplicate),
    )


def _check_equivalent_mcp_table_format_is_idempotent(root: Path, clone: Path) -> None:
    config_path = root / "equivalent-config.toml"
    generated = _mcp_block("testhum", clone)
    equivalent = generated.replace(
        'command = "' + str(clone / ".venv" / "bin" / "python3") + '"\n'
        'args = ["-m", "agent_messaging_plugin.mcp_bridge"]\n'
        'env_vars = ["CODEX_THREAD_ID"]\n',
        'env_vars = ["CODEX_THREAD_ID"]\n'
        'args = ["-m", "agent_messaging_plugin.mcp_bridge"]\n'
        'command = "' + str(clone / ".venv" / "bin" / "python3") + '"\n',
    )
    _check("the equivalent MCP fixture differs textually", equivalent != generated)
    config_path.write_text(equivalent, encoding="utf-8")
    result = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=root / "equivalent-bin", codex_config_path=config_path,
    )
    _check(
        "a semantically equivalent differently formatted MCP table is already installed",
        result.mcp_status == "already_installed" and config_path.read_text(encoding="utf-8") == equivalent,
        repr(result),
    )


def _check_stale_symlink_repoint(root: Path, clone: Path, bin_dir: Path, target: Path) -> None:
    other_clone = _make_clone(root, name="other_clone")
    launcher = bin_dir / "testhum"
    launcher.unlink()
    launcher.symlink_to(other_clone / ".venv" / "bin" / CONSOLE_SCRIPT_NAME)
    repointed = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=bin_dir,
        codex_config_path=root / "codex-repoint" / "config.toml",
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
            codex_config_path=root / "occupied-config.toml",
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
            codex_config_path=root / "missing-config.toml",
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
            codex_config_path=root / "escape-config.toml",
        )
        raise SmokeFailureError("invalid name did not raise")
    except CommandLauncherError as exc:
        _check(
            "an invalid solet name is refused before touching bin_dir",
            "invalid solet name" in str(exc),
            str(exc),
        )
    _check(
        "the refused name created nothing outside bin_dir",
        not (root / "escape").exists(),
    )

    different_config = root / "different-config.toml"
    different_config.write_text(
        '[mcp_servers.testhum]\ncommand = "/operator/python"\n', encoding="utf-8",
    )
    try:
        install_command_launcher_at_birth(
            name="testhum", clone_root=clone, bin_dir=root / "different-bin",
            codex_config_path=different_config,
        )
        raise SmokeFailureError("different MCP table did not raise")
    except CommandLauncherError as exc:
        _check(
            "a different same-name Codex MCP table is refused with differing keys only",
            "refusing to overwrite" in str(exc) and "keys: " in str(exc) and "command" in str(exc),
            str(exc),
        )


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone, bin_dir, config_path, target = _check_fresh_install(root)
            _check_self_generated_config_is_idempotent(clone, bin_dir, config_path, target)
            _check_equivalent_mcp_table_format_is_idempotent(root, clone)
            _check_stale_symlink_repoint(root, clone, bin_dir, target)
            _check_failure_modes(root)
    except SmokeFailureError as exc:
        print(f"command_launcher_smoke FAILED: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"command_launcher_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
