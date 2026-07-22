"""Uninstall the local blue-green router from launchd (macOS) or systemd (Linux).

Per L3 plan §3.6 (`workbench/2026-06-01_local_blue_green_L3_implementation_plan.md`).

Symmetric to install_router.py. Idempotent: re-running on an already-uninstalled
system is a no-op success. Fast-fail on any unexpected supervisor exit code.

Usage:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/uninstall_router.py <homunculus_name>

Path overrides (smoke harness only):
    --plist-path <PATH>
    --unit-path <PATH>
    --socket-path <PATH>
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

# Self-bootstrap so the script runs from any CWD; the deployment/ namespace
# package only resolves when the repo root is on sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from macos_self_deployment_plugin.blue_green_router.service_install import (  # noqa: E402
    InstallError,
    default_launchd_plist_path,
    default_socket_path,
    default_systemd_unit_path,
    launchd_label,
    systemd_unit_name,
    validate_homunculus_name,
)

SOCKET_CLEANUP_DEADLINE_SECONDS: float = 5.0
SOCKET_CLEANUP_POLL_INTERVAL_SECONDS: float = 0.1


def _remove_router_port_file(homunculus_name: str) -> None:
    """Remove the router-port discovery file written by install_router.

    Idempotent: missing file is a no-op success.
    """
    port_file = Path.home() / ".ananta" / "runtime" / f"{homunculus_name}.router.port"
    if port_file.exists():
        port_file.unlink()

# launchctl exits 3 (ESRCH "No such process") when bootout-ing a label that
# isn't loaded. Treat it as the no-op success we want for idempotent uninstall.
LAUNCHCTL_BOOTOUT_NOT_LOADED_EXIT: int = 3
SYSTEMCTL_NOT_LOADED_EXIT: int = 5


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        validate_homunculus_name(args.homunculus_name)
        system_name = platform.system()
        if system_name == "Darwin":
            _uninstall_launchd(args)
        elif system_name == "Linux":
            _uninstall_systemd(args)
        else:
            raise InstallError(
                f"unsupported platform {system_name!r}; supported: Darwin, Linux",
            )
        socket_path = args.socket_path or default_socket_path(args.homunculus_name)
        _verify_socket_gone(socket_path)
        _remove_router_port_file(args.homunculus_name)
    except InstallError as exc:
        print(f"uninstall_router: {exc}", file=sys.stderr)
        return 1
    print(
        f"uninstall_router: OK — router for homunculus={args.homunculus_name!r} "
        "is stopped and removed",
    )
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="uninstall_router",
        description=(
            "Uninstall the local blue-green router from launchd/systemd. "
            "Idempotent: re-running on an uninstalled system is a no-op success."
        ),
    )
    parser.add_argument("homunculus_name", help="Homunculus name (e.g. 'iris').")
    parser.add_argument(
        "--plist-path", type=Path, default=None,
        help="Override default ~/Library/LaunchAgents/<label>.plist (smoke only).",
    )
    parser.add_argument(
        "--unit-path", type=Path, default=None,
        help="Override default ~/.config/systemd/user/<unit>.service (smoke only).",
    )
    parser.add_argument(
        "--socket-path", type=Path, default=None,
        help="Override default ~/.ananta/runtime/<name>.router.sock (smoke only).",
    )
    return parser.parse_args(argv)


def _uninstall_launchd(args: argparse.Namespace) -> None:
    plist_path = args.plist_path or default_launchd_plist_path(args.homunculus_name)
    label = launchd_label(args.homunculus_name)
    service_target = f"gui/{os.getuid()}/{label}"
    # Bootout first so KeepAlive can't respawn after we unlink the plist.
    _run_launchctl(
        ["bootout", service_target],
        allow_exit_codes={0, LAUNCHCTL_BOOTOUT_NOT_LOADED_EXIT},
    )
    plist_path.unlink(missing_ok=True)


def _uninstall_systemd(args: argparse.Namespace) -> None:
    unit_path = args.unit_path or default_systemd_unit_path(args.homunculus_name)
    unit_name = systemd_unit_name(args.homunculus_name)
    _run_systemctl(
        ["disable", "--now", unit_name],
        allow_exit_codes={0, SYSTEMCTL_NOT_LOADED_EXIT},
    )
    unit_path.unlink(missing_ok=True)
    _run_systemctl(["daemon-reload"], allow_exit_codes={0})


def _run_launchctl(args: list[str], *, allow_exit_codes: set[int]) -> subprocess.CompletedProcess[str]:
    return _run_supervisor("launchctl", args, allow_exit_codes=allow_exit_codes)


def _run_systemctl(args: list[str], *, allow_exit_codes: set[int]) -> subprocess.CompletedProcess[str]:
    return _run_supervisor("systemctl", ["--user", *args], allow_exit_codes=allow_exit_codes)


def _run_supervisor(
    program: str,
    args: list[str],
    *,
    allow_exit_codes: set[int],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [program, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in allow_exit_codes:
        raise InstallError(
            f"{program} {' '.join(args)} exited {result.returncode}\n"
            f"stdout: {result.stdout.rstrip()}\n"
            f"stderr: {result.stderr.rstrip()}",
        )
    return result


def _verify_socket_gone(socket_path: Path) -> None:
    deadline = time.monotonic() + SOCKET_CLEANUP_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if not socket_path.exists():
            return
        time.sleep(SOCKET_CLEANUP_POLL_INTERVAL_SECONDS)
    raise InstallError(
        f"socket {socket_path} still present after {SOCKET_CLEANUP_DEADLINE_SECONDS}s "
        "— supervisor reported stop but router did not unlink its socket",
    )


if __name__ == "__main__":
    raise SystemExit(main())
