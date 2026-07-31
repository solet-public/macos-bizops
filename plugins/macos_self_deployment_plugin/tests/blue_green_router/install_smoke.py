"""Slice H install/uninstall smoke — round-trip against real launchd/systemd.

Per L3 plan §3.6 smoke contract:

  install on a clean state → verify router process running + mgmt socket
  present + status() returns clean → uninstall → verify process stopped
  + socket cleaned.

Sandbox discipline (per `[[sandbox_mutating_smokes]]`):

  - smoke homunculus name 'bgsmoke' → label `local.homunculus.bgsmoke.router` /
    unit `local.homunculus.bgsmoke.router.service`. Distinct from any real install.
  - plist/unit written to a tmp dir (not LaunchAgents/systemd user dir).
  - socket placed in a tmp runtime dir (not ~/.ananta/runtime/).
  - non-canonical public_port (free high port) so no collision with a
    a real homunculus's router on 8100.
  - try/finally guarantees uninstall fires even on mid-flight failure,
    so a failed smoke can never leave a KeepAlive-respawning phantom
    agent registered in launchd.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/blue_green_router/install_smoke.py
"""

from __future__ import annotations

import json
import os
import socket as socket_mod
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from macos_self_deployment_plugin.blue_green_router.service_install import (  # noqa: E402
    launchd_label,
    systemd_unit_name,
)

# Canonical installer/uninstaller location. F2 Choice Y deleted the old
# ``deployment/local_blue_green/`` tree; the scripts now live inside the
# plugin package. Computed from the plugin root (parents[2]) so it does not
# depend on the (vestigial, plugins-dir-valued) REPO_ROOT above.
_ROUTER_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "macos_self_deployment_plugin" / "blue_green_router"
)
_INSTALL_ROUTER = _ROUTER_SCRIPTS_DIR / "install_router.py"
_UNINSTALL_ROUTER = _ROUTER_SCRIPTS_DIR / "uninstall_router.py"

SMOKE_HOMUNCULUS_NAME = "bgsmoke"
LAUNCHCTL_BOOTOUT_NOT_LOADED_EXIT = 113


def _stamp(label: str, ok: bool, detail: str = "") -> bool:
    sym = "PASS" if ok else "FAIL"
    print(f"  [{sym}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def _pick_free_port() -> int:
    with socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _mgmt_status(socket_path: Path) -> dict[str, object]:
    with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(str(socket_path))
        sock.sendall(json.dumps({"verb": "status", "args": {}}).encode() + b"\n")
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
    return json.loads(bytes(buf).split(b"\n", 1)[0].decode())


def _run_install(
    *,
    plist_path: Path,
    unit_path: Path,
    socket_path: Path,
    log_dir: Path,
    public_port: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_INSTALL_ROUTER),
            SMOKE_HOMUNCULUS_NAME,
            "--public-port", str(public_port),
            "--plist-path", str(plist_path),
            "--unit-path", str(unit_path),
            "--socket-path", str(socket_path),
            "--log-dir", str(log_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_uninstall(
    *,
    plist_path: Path,
    unit_path: Path,
    socket_path: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_UNINSTALL_ROUTER),
            SMOKE_HOMUNCULUS_NAME,
            "--plist-path", str(plist_path),
            "--unit-path", str(unit_path),
            "--socket-path", str(socket_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _belt_and_suspenders_bootout() -> None:
    """Best-effort bootout of the smoke label in case a previous smoke run
    crashed before the finally-uninstall fired. Tolerates 'not loaded'."""

    import platform

    if platform.system() != "Darwin":
        return
    service_target = f"gui/{os.getuid()}/{launchd_label(SMOKE_HOMUNCULUS_NAME)}"
    subprocess.run(
        ["launchctl", "bootout", service_target],
        capture_output=True,
        text=True,
        check=False,
    )


def _case_install_clean(
    *,
    plist_path: Path,
    unit_path: Path,
    socket_path: Path,
    log_dir: Path,
    public_port: int,
) -> bool:
    print("\n[case_install_clean] fresh install on empty state")
    proc = _run_install(
        plist_path=plist_path, unit_path=unit_path,
        socket_path=socket_path, log_dir=log_dir, public_port=public_port,
    )
    ok_install = proc.returncode == 0
    if not _stamp("install exit 0", ok_install, f"stderr={proc.stderr!r}"):
        return False
    ok_socket = socket_path.exists()
    _stamp("mgmt socket present", ok_socket, str(socket_path))
    status: dict[str, object] | None = None
    if ok_socket:
        try:
            status = _mgmt_status(socket_path)
        except (OSError, ValueError) as exc:
            _stamp("mgmt status() reachable", False, repr(exc))
    ok_status = status is not None and "router_started_at" in status
    _stamp("mgmt status() returns router_started_at", ok_status, repr(status))
    ok_plist_written = plist_path.exists() or unit_path.exists()
    _stamp("plist or unit written to expected path", ok_plist_written)
    return ok_install and ok_socket and ok_status and ok_plist_written


def _case_install_idempotent(
    *,
    plist_path: Path,
    unit_path: Path,
    socket_path: Path,
    log_dir: Path,
    public_port: int,
) -> bool:
    print("\n[case_install_idempotent] re-run install on already-loaded state")
    proc = _run_install(
        plist_path=plist_path, unit_path=unit_path,
        socket_path=socket_path, log_dir=log_dir, public_port=public_port,
    )
    ok_reinstall = proc.returncode == 0
    _stamp(
        "re-install exit 0 (idempotent)",
        ok_reinstall,
        f"stderr={proc.stderr!r}" if not ok_reinstall else "",
    )
    ok_socket_still = socket_path.exists()
    _stamp("socket still present after re-install", ok_socket_still)
    return ok_reinstall and ok_socket_still


def _case_uninstall_clean(
    *,
    plist_path: Path,
    unit_path: Path,
    socket_path: Path,
) -> bool:
    print("\n[case_uninstall_clean] uninstall after install")
    proc = _run_uninstall(
        plist_path=plist_path, unit_path=unit_path, socket_path=socket_path,
    )
    ok_uninstall = proc.returncode == 0
    _stamp(
        "uninstall exit 0",
        ok_uninstall,
        f"stderr={proc.stderr!r}" if not ok_uninstall else "",
    )
    # Give the supervisor a beat to actually kill the process and unlink.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and socket_path.exists():
        time.sleep(0.1)
    ok_socket_gone = not socket_path.exists()
    _stamp("socket cleaned up", ok_socket_gone, str(socket_path))
    ok_plist_gone = not plist_path.exists() and not unit_path.exists()
    _stamp("plist/unit file removed", ok_plist_gone)
    return ok_uninstall and ok_socket_gone and ok_plist_gone


def _case_uninstall_idempotent(
    *,
    plist_path: Path,
    unit_path: Path,
    socket_path: Path,
) -> bool:
    print("\n[case_uninstall_idempotent] re-run uninstall on empty state")
    proc = _run_uninstall(
        plist_path=plist_path, unit_path=unit_path, socket_path=socket_path,
    )
    ok_reuninstall = proc.returncode == 0
    _stamp(
        "re-uninstall exit 0 (idempotent)",
        ok_reuninstall,
        f"stderr={proc.stderr!r}" if not ok_reuninstall else "",
    )
    return ok_reuninstall


def main() -> int:
    import platform

    if platform.system() not in ("Darwin", "Linux"):
        print(f"smoke skip: unsupported platform {platform.system()}")
        return 0

    _belt_and_suspenders_bootout()
    tmpdir = Path(tempfile.mkdtemp(prefix="bg-install-smoke-"))
    plist_dir = tmpdir / "LaunchAgents"
    plist_dir.mkdir()
    unit_dir = tmpdir / "systemd"
    unit_dir.mkdir()
    runtime_dir = tmpdir / "runtime"
    runtime_dir.mkdir()
    log_dir = tmpdir / "logs"
    log_dir.mkdir()
    plist_path = plist_dir / f"{launchd_label(SMOKE_HOMUNCULUS_NAME)}.plist"
    unit_path = unit_dir / systemd_unit_name(SMOKE_HOMUNCULUS_NAME)
    socket_path = runtime_dir / f"{SMOKE_HOMUNCULUS_NAME}.router.sock"
    public_port = _pick_free_port()

    print(
        f"install_smoke: tmp={tmpdir} port={public_port} "
        f"label={launchd_label(SMOKE_HOMUNCULUS_NAME)} "
        f"unit={systemd_unit_name(SMOKE_HOMUNCULUS_NAME)}"
    )

    results: list[tuple[str, bool]] = []
    try:
        results.append(("install_clean", _case_install_clean(
            plist_path=plist_path, unit_path=unit_path,
            socket_path=socket_path, log_dir=log_dir, public_port=public_port,
        )))
        results.append(("install_idempotent", _case_install_idempotent(
            plist_path=plist_path, unit_path=unit_path,
            socket_path=socket_path, log_dir=log_dir, public_port=public_port,
        )))
        results.append(("uninstall_clean", _case_uninstall_clean(
            plist_path=plist_path, unit_path=unit_path, socket_path=socket_path,
        )))
        results.append(("uninstall_idempotent", _case_uninstall_idempotent(
            plist_path=plist_path, unit_path=unit_path, socket_path=socket_path,
        )))
    finally:
        # Defense in depth: even if assertions failed mid-flight, ensure
        # the launchd label can't keep respawning a smoke router on the
        # operator's machine.
        _run_uninstall(
            plist_path=plist_path, unit_path=unit_path, socket_path=socket_path,
        )
        _belt_and_suspenders_bootout()

    print("\nsummary")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        _stamp(name, ok)
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
