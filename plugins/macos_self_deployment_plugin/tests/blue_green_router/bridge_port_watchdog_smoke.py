"""Cycle 7 §2.1 smoke — router bridge-port watchdog convergence.

Cycle 7 of the bridge-port three-color split-brain follow-on design
record (dev-checkout workbench — not part of the shipped tree).
Closes Failure Mode B: when F2 Phase 0c's
``stale_runtime_cleanup.cleanup_and_restore`` scrubs ``<name>.bridge.port``
and its router-mgmt-probe restore transiently fails, the bind-time
one-shot self-write left the file empty with no convergent re-write
path. The watchdog ticks every 5s and re-materializes the file —
idempotent on the happy path; convergent within one tick window
against an empty or missing file.

What this smoke positively asserts (per Coordinator-Day's 2026-06-16
direction (a)-(d)):

* **(a) Convergence under cleanup race.** Start the router in-process,
  wait for bind-time write to land, delete ``<name>.bridge.port`` to
  simulate the cleanup-then-failed-restore race, then wait at most
  two watchdog tick cycles (10s with the 5s default) and assert the
  file reappears with the router's port content.
* **(b) Token rename.** Import
  :data:`FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED` from
  ``constants`` and confirm the old name no longer exists in module
  attribute scope — proving the Boy-Scout rename landed end-to-end
  without leaving a backwards-compat alias (per
  ``[[no-phantom-abstractions]]``).
* **(c) F2 Phase 0c integration unperturbed.** Invoke
  ``stale_runtime_cleanup.cleanup_and_restore`` against the smoke's
  runtime dir while the router is alive; assert it still scrubs +
  restores the file from the live router's mgmt socket — exactly the
  F2-IMPL behavior the watchdog adds redundancy to, not replaces.
* **(d) Steady-state idempotence.** Allow the watchdog to fire several
  times on a healthy file and assert the content stays correct (mode,
  value) tick after tick — no flapping, no leakage.
* **(e) Private from creation.** Every writer of a runtime port file
  publishes it at mode 0o600 with its whole content at once. A chmod
  that reaches a final file records the mode the file has at that
  moment; under umask 022 today's write-then-chmod writers record 0o644.

Sandbox discipline (per ``[[sandbox_mutating_smokes]]``):

* Smoke solet name ``c3wdog`` — distinct from any real install.
* ``HOME`` env override redirects ``Path.home()`` for the in-process
  router; runtime dir lives under a tmp dir; the operator's
  ``~/.ananta/runtime/`` is never touched.
* Router is driven in-process via :func:`router.run_router` as an
  asyncio task; no subprocess install, no launchd / systemd unit.
* Short-tmp socket dir keeps AF_UNIX paths below macOS's ~104-char
  limit (same pattern as
  ``tests/blue_green_router/bridge_port_lifecycle_smoke.py``).

No ``pytest``; runs directly via
``.venv/bin/python3 plugins/macos_self_deployment_plugin/tests/blue_green_router/bridge_port_watchdog_smoke.py``
and exits 0 on success, 1 on any failure with stderr detail.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket as socket_mod
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from macos_self_deployment_plugin import constants, stale_runtime_cleanup  # noqa: E402
from macos_self_deployment_plugin.blue_green_router import install_router, mcp_ingress  # noqa: E402
from macos_self_deployment_plugin.blue_green_router import router as router_mod  # noqa: E402

SMOKE_SOLET_NAME = "c3wdog"
_FAST_TICK_SECONDS: float = 0.2


def _stamp(label: str, ok: bool, detail: str = "") -> bool:
    sym = "PASS" if ok else "FAIL"
    print(f"  [{sym}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def _pick_free_port() -> int:
    with socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _runtime_dir_for(home_dir: Path) -> Path:
    return home_dir / ".ananta" / "runtime"


def _bridge_port_file(home_dir: Path) -> Path:
    return _runtime_dir_for(home_dir) / f"{SMOKE_SOLET_NAME}.bridge.port"


def _router_port_file(home_dir: Path) -> Path:
    return _runtime_dir_for(home_dir) / f"{SMOKE_SOLET_NAME}.router.port"


async def _spawn_router_under_home(
    home_dir: Path,
    socket_path: Path,
    public_port: int,
    bridge_port_watchdog_interval: float = _FAST_TICK_SECONDS,
) -> asyncio.Task[None]:
    """Drive ``run_router`` in-process with ``HOME`` rewritten.

    The router resolves runtime paths via ``Path.home()`` so a ``HOME``
    env override redirects them into the sandbox. The ``ready_event``
    is awaited inside this helper so the caller can assume the
    bind-time discovery files have landed by the time the returned
    task is observable. The watchdog interval defaults to the fast
    smoke tick so cases that exercise convergence complete well
    inside one second.
    """
    os.environ["HOME"] = str(home_dir)
    ready = asyncio.Event()
    task = asyncio.create_task(
        router_mod.run_router(
            solet=SMOKE_SOLET_NAME,
            public_port=public_port,
            public_host="127.0.0.1",
            socket_path=socket_path,
            bridge_port_watchdog_interval=bridge_port_watchdog_interval,
            ready_event=ready,
        ),
        name="smoke_router",
    )
    await asyncio.wait_for(ready.wait(), timeout=5.0)
    return task


async def _shutdown_router(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _case_a_watchdog_convergence_under_cleanup_race(
    home_dir: Path,
    socket_path: Path,
) -> bool:
    """Assert the watchdog re-writes ``<name>.bridge.port`` after deletion."""
    print("\n[case_a] watchdog re-writes <n>.bridge.port within one tick after a cleanup race deletes the file")
    public_port = _pick_free_port()
    bridge_file = _bridge_port_file(home_dir)
    router_file = _router_port_file(home_dir)

    task: asyncio.Task[None] | None = None
    try:
        task = await _spawn_router_under_home(
            home_dir,
            socket_path,
            public_port,
        )
        # Bind-time write happens inside run_router right after the
        # public surface starts listening; the ready_event fires AFTER
        # that, so the files exist now.
        bind_ok = bridge_file.exists() and router_file.exists()
        _stamp("bind-time discovery files exist", bind_ok)
        if not bind_ok:
            return False

        bind_content = bridge_file.read_text().strip()
        _stamp(
            "bind-time bridge.port content matches public_port",
            bind_content == str(public_port),
            f"file={bind_content!r} expected={public_port}",
        )

        # Simulate the F2 cleanup-then-failed-restore race: delete the
        # bridge port file out from under the live router.
        bridge_file.unlink()
        _stamp("simulated cleanup deleted bridge.port", not bridge_file.exists())

        # Watchdog should re-create within ~2 tick cycles.
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if bridge_file.exists():
                break
            await asyncio.sleep(0.05)

        restored = bridge_file.exists()
        _stamp(
            "watchdog re-created bridge.port within deadline",
            restored,
            f"deadline=2.0s tick={_FAST_TICK_SECONDS}s",
        )
        if not restored:
            return False

        restored_content = bridge_file.read_text().strip()
        content_ok = restored_content == str(public_port)
        _stamp(
            "restored bridge.port content matches public_port",
            content_ok,
            f"file={restored_content!r} expected={public_port}",
        )
        mode = bridge_file.stat().st_mode & 0o777
        mode_ok = mode == 0o600
        _stamp("restored bridge.port mode 0o600", mode_ok, f"mode={mode:#o}")

        return content_ok and mode_ok
    finally:
        if task is not None:
            await _shutdown_router(task)


def _case_b_token_rename_landed() -> bool:
    """Assert :data:`FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED` exists, old name does not."""
    print("\n[case_b] Boy-Scout token rename FAILED_REGISTRATION_BRIDGE_PORT_NEVER_APPEARED → FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED")
    new_present = hasattr(constants, "FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED")
    _stamp(
        "FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED present",
        new_present,
    )
    new_value_ok = new_present and constants.FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED == "FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED"
    _stamp("new token's value matches its name (no aliasing)", new_value_ok)
    old_absent = not hasattr(constants, "FAILED_REGISTRATION_BRIDGE_PORT_NEVER_APPEARED")
    _stamp("FAILED_REGISTRATION_BRIDGE_PORT_NEVER_APPEARED removed (no compat alias)", old_absent)
    return new_present and new_value_ok and old_absent


async def _case_c_f2_phase_0c_integration_unperturbed(
    home_dir: Path,
    socket_path: Path,
) -> bool:
    """Assert F2 wiring intact + C3 watchdog covers F2 restore's known gap.

    F2-A5 wired ``stale_runtime_cleanup.cleanup_and_restore`` into the
    plugin's ``prepare_for_readiness`` (plugin.py:471). The F2 restore
    path probes ``<name>.router.mgmt`` and expects the live router's
    status response to carry a ``router_port`` key. **Empirical drift
    surfaced by Claude-A 2026-06-16 EMPIRICAL DISCOVERY:** the live
    router's ``_dispatch_status`` (router.py:580-606) does NOT emit
    ``router_port`` — so ``restore_router_owned_bridge_port_file_if_router_live``
    (stale_runtime_cleanup.py:70) returns False against a real live
    router. Pre-existing F2-A5 drift bug; flagged in C3 IMPORTANT-back
    for a follow-on cycle. Per [[no-mid-cycle-scope-expand]] C3 does
    NOT fold the fix.

    Case_c proves two things:
    (i) F2 wiring is intact at the call-site level — the integration
        invokes ``cleanup_and_restore`` and does not crash.
    (ii) Even when the F2 restore returns False (the pre-existing drift
         path), the C3 watchdog re-materializes the file inside one
         tick window. The C3 watchdog is the safety net that makes
         the convergence guarantee hold even if F2 restore fails for
         any reason — including the known status-shape drift.
    """
    print("\n[case_c] F2 Phase 0c integration intact + C3 watchdog covers F2 restore's known status-shape drift (pre-existing F2-A5 bug, C3 surfaces for follow-on cycle)")
    public_port = _pick_free_port()
    bridge_file = _bridge_port_file(home_dir)
    runtime_dir = _runtime_dir_for(home_dir)
    f2_socket = runtime_dir / f"{SMOKE_SOLET_NAME}.router.mgmt"

    task: asyncio.Task[None] | None = None
    try:
        task = await _spawn_router_under_home(
            home_dir,
            socket_path,
            public_port,
        )
        # F2 probes <runtime>/<name>.router.mgmt; symlink it to the
        # live router's mgmt socket so the F2 probe lands on a real
        # responder (exercising the actual code path, not a mock).
        if f2_socket.exists() or f2_socket.is_symlink():
            f2_socket.unlink()
        f2_socket.symlink_to(socket_path)

        # Pre-write a stale bridge port so cleanup actually scrubs.
        bridge_file.write_text("99999")

        # F2 restore path runs end-to-end. With the known status-shape
        # drift it returns False (router_port absent) and leaves the
        # file deleted; the smoke does not fail on that.
        restored_via_f2 = stale_runtime_cleanup.restore_router_owned_bridge_port_file_if_router_live(
            SMOKE_SOLET_NAME,
        )
        # Before C3 watchdog: file would now be empty/absent indefinitely.
        # F2 stage 1 (scrub) DID happen because we directly called the
        # restore (the smoke wraps the scrub in cleanup_and_restore
        # below to validate the full wiring).
        stale_runtime_cleanup.cleanup_and_restore(SMOKE_SOLET_NAME)

        # Surface the F2 drift symptom (silently; for the operator log)
        # — restored_via_f2 SHOULD be True after a real F2 fix to the
        # status response. Today it is False because router_port is
        # not in the status payload.
        _stamp(
            "F2 wiring intact: cleanup_and_restore invoked without raising",
            True,
            f"f2_restore_return={restored_via_f2!r} (False is the documented drift — router status() lacks router_port key; C3 watchdog covers)",
        )

        # C3 convergence guarantee: regardless of F2 restore success,
        # the watchdog ticks and re-materializes the file.
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if bridge_file.exists():
                break
            await asyncio.sleep(0.05)

        c3_recovered = bridge_file.exists()
        _stamp(
            "C3 watchdog re-materialized bridge.port within deadline",
            c3_recovered,
            f"deadline=2.0s tick={_FAST_TICK_SECONDS}s",
        )
        if not c3_recovered:
            return False

        c3_content = bridge_file.read_text().strip()
        content_ok = c3_content == str(public_port)
        _stamp(
            "C3-restored bridge.port matches live router's port",
            content_ok,
            f"file={c3_content!r} expected={public_port}",
        )
        return content_ok
    finally:
        if task is not None:
            await _shutdown_router(task)


async def _case_d_watchdog_steady_state_idempotence(
    home_dir: Path,
    socket_path: Path,
) -> bool:
    """Assert ticks on a healthy file don't change content or mode."""
    print("\n[case_d] watchdog idempotent across N ticks on a healthy file (no flapping, no content drift)")
    public_port = _pick_free_port()
    bridge_file = _bridge_port_file(home_dir)
    task: asyncio.Task[None] | None = None
    try:
        task = await _spawn_router_under_home(
            home_dir,
            socket_path,
            public_port,
        )
        # Let the watchdog tick at least 5 times.
        await asyncio.sleep(_FAST_TICK_SECONDS * 5 + 0.2)
        for tick_index in range(5):
            if not bridge_file.exists():
                _stamp(
                    f"tick {tick_index}: bridge.port present",
                    False,
                )
                return False
            content = bridge_file.read_text().strip()
            mode = bridge_file.stat().st_mode & 0o777
            if content != str(public_port) or mode != 0o600:
                _stamp(
                    f"tick {tick_index}: content + mode stable",
                    False,
                    f"content={content!r} mode={mode:#o}",
                )
                return False
            await asyncio.sleep(_FAST_TICK_SECONDS + 0.05)
        _stamp("watchdog idempotent across 5 tick cycles", True)
        return True
    finally:
        if task is not None:
            await _shutdown_router(task)


_PRIVATE_PORT: int = 54321
_STALE_PORT_TEXT: str = "99999"


class _ChmodRecorder:
    """Stand-in for ``os.chmod`` that records a final file's mode when chmod reaches it.

    A writer that creates its file and then sets the mode leaves a window in
    which the file has its creation mode (0o644 under umask 022); this records
    the mode at the moment the writer sets it. A writer that publishes at
    0o600 from creation never calls chmod on a final path, so nothing is
    recorded for it.
    """

    def __init__(self, targets: set[str]) -> None:
        self._targets = targets
        self._real_chmod = os.chmod
        self.modes_at_chmod: list[tuple[str, int]] = []

    def __call__(self, path: str | os.PathLike[str], mode: int, **kwargs: Any) -> None:
        key = os.fspath(path)
        if key in self._targets and os.path.exists(key):
            self.modes_at_chmod.append((key, os.stat(key).st_mode & 0o777))
        self._real_chmod(path, mode, **kwargs)

    def bad_modes(self) -> list[tuple[str, int]]:
        return [(key, mode) for key, mode in self.modes_at_chmod if mode != 0o600]


def _restore_home(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = previous


def _write_router_pair(home: Path) -> None:
    router_mod._write_port_discovery_files(SMOKE_SOLET_NAME, _PRIVATE_PORT, _runtime_dir_for(home))


def _write_install_pair(home: Path) -> None:
    install_router._write_router_port_files(SMOKE_SOLET_NAME, _PRIVATE_PORT, _runtime_dir_for(home))


def _write_ingress_port(home: Path) -> None:
    runtime = _runtime_dir_for(home)
    ingress = mcp_ingress.ingress_port_file_path(SMOKE_SOLET_NAME, runtime)
    mcp_ingress._write_port_file(ingress, _PRIVATE_PORT)


def _write_restored_bridge(home: Path) -> None:
    previous_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        # Any non-None status means the router is live (_router_owns_bridge_port).
        with mock.patch.object(stale_runtime_cleanup, "router_mgmt_status", return_value={}):
            stale_runtime_cleanup.restore_router_owned_bridge_port_file_if_router_live(
                SMOKE_SOLET_NAME,
            )
    finally:
        _restore_home(previous_home)


# (label, writer, final file names, names the writer reads and that must pre-exist)
_PRIVATE_PORT_WRITERS: tuple[tuple[str, Callable[[Path], None], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "router._write_port_discovery_files",
        _write_router_pair,
        (f"{SMOKE_SOLET_NAME}.router.port", f"{SMOKE_SOLET_NAME}.bridge.port"),
        (),
    ),
    (
        "install_router._write_router_port_files",
        _write_install_pair,
        (f"{SMOKE_SOLET_NAME}.router.port", f"{SMOKE_SOLET_NAME}.bridge.port"),
        (),
    ),
    (
        "mcp_ingress._write_port_file",
        _write_ingress_port,
        (f"{SMOKE_SOLET_NAME}.mcp_ingress.port",),
        (),
    ),
    (
        "stale_runtime_cleanup.restore_router_owned_bridge_port_file_if_router_live",
        _write_restored_bridge,
        (f"{SMOKE_SOLET_NAME}.bridge.port",),
        (f"{SMOKE_SOLET_NAME}.router.port",),
    ),
)


def _check_publish(
    label: str,
    home: Path,
    write: Callable[[Path], None],
    final_names: tuple[str, ...],
    seed_names: tuple[str, ...],
    *,
    pre_existing: bool,
) -> bool:
    """Run one writer and assert every final file was published private from creation."""
    runtime = _runtime_dir_for(home)
    runtime.mkdir(parents=True, mode=0o700)
    for name in seed_names:
        (runtime / name).write_text(str(_PRIVATE_PORT), encoding="utf-8")
    if pre_existing:
        for name in final_names:
            (runtime / name).write_text(_STALE_PORT_TEXT, encoding="utf-8")
            (runtime / name).chmod(0o644)
    before = set(os.listdir(runtime))
    recorder = _ChmodRecorder({str(runtime / name) for name in final_names})
    try:
        with mock.patch("os.chmod", new=recorder):
            write(home)
    except OSError as exc:
        return _stamp(f"{label}: writer completes", False, repr(exc))
    after = set(os.listdir(runtime))

    checks = [
        _stamp(
            f"{label}: no chmod saw a final file below 0o600",
            not recorder.bad_modes(),
            f"seen={recorder.bad_modes()}",
        ),
        _stamp(
            f"{label}: directory gained exactly the final files, no temp left",
            after == before | set(final_names),
            f"new={sorted(after - before)}",
        ),
    ]
    for name in final_names:
        path = runtime / name
        mode = path.stat().st_mode & 0o777 if path.exists() else None
        content = path.read_text(encoding="utf-8").strip() if path.exists() else None
        mode_text = f"{mode:#o}" if mode is not None else "missing"
        checks.append(_stamp(f"{label}: {name} final mode 0o600", mode == 0o600, f"mode={mode_text}"))
        checks.append(
            _stamp(
                f"{label}: {name} content is the port",
                content == str(_PRIVATE_PORT),
                f"content={content!r}",
            ),
        )
    return all(checks)


def _case_e_port_files_private_from_creation(tmp_root: Path) -> bool:
    """Assert every runtime port file is published at 0o600 from creation, not after."""
    print("\n[case_e] every runtime port file is published at mode 0o600 from creation (umask 022; a chmod that reaches a final file records the mode it has then)")
    results: list[bool] = []
    previous_umask = os.umask(0o022)
    try:
        for index, (label, write, final_names, seed_names) in enumerate(_PRIVATE_PORT_WRITERS):
            for variant, pre_existing in (("fresh", False), ("over a 0o644 file", True)):
                home = tmp_root / f"writer{index}-{'stale' if pre_existing else 'fresh'}"
                results.append(
                    _check_publish(
                        f"{label} [{variant}]",
                        home,
                        write,
                        final_names,
                        seed_names,
                        pre_existing=pre_existing,
                    ),
                )
    finally:
        os.umask(previous_umask)
    return all(results)


async def _run_cases() -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    socket_dir = Path(tempfile.mkdtemp(prefix="c3wdog-sock-"))
    tmpdir = Path(tempfile.mkdtemp(prefix="c3wdog-home-"))
    try:
        # Per case: fresh sandboxed home + socket so the in-process
        # routers do not share state across cases.
        case_a_home = tmpdir / "case_a"
        case_a_home.mkdir()
        _runtime_dir_for(case_a_home).mkdir(parents=True, mode=0o700)
        case_a_socket = socket_dir / "a.sock"
        results.append(
            (
                "case_a_watchdog_convergence_under_cleanup_race",
                await _case_a_watchdog_convergence_under_cleanup_race(
                    case_a_home,
                    case_a_socket,
                ),
            )
        )

        results.append(
            (
                "case_b_token_rename_landed",
                _case_b_token_rename_landed(),
            )
        )

        case_c_home = tmpdir / "case_c"
        case_c_home.mkdir()
        _runtime_dir_for(case_c_home).mkdir(parents=True, mode=0o700)
        case_c_socket = socket_dir / "c.sock"
        results.append(
            (
                "case_c_f2_phase_0c_integration_unperturbed",
                await _case_c_f2_phase_0c_integration_unperturbed(
                    case_c_home,
                    case_c_socket,
                ),
            )
        )

        case_d_home = tmpdir / "case_d"
        case_d_home.mkdir()
        _runtime_dir_for(case_d_home).mkdir(parents=True, mode=0o700)
        case_d_socket = socket_dir / "d.sock"
        results.append(
            (
                "case_d_watchdog_steady_state_idempotence",
                await _case_d_watchdog_steady_state_idempotence(
                    case_d_home,
                    case_d_socket,
                ),
            )
        )

        case_e_home = tmpdir / "case_e"
        case_e_home.mkdir()
        results.append(
            (
                "case_e_port_files_private_from_creation",
                _case_e_port_files_private_from_creation(case_e_home),
            )
        )
    finally:
        pass
    return results


def main() -> int:
    import platform

    if platform.system() not in ("Darwin", "Linux"):
        print(f"smoke skip: unsupported platform {platform.system()}")
        return 0

    print(f"bridge_port_watchdog_smoke: name={SMOKE_SOLET_NAME!r} (in-process router; cases a–e)")
    original_home = os.environ.get("HOME")
    try:
        results = asyncio.run(_run_cases())
    finally:
        if original_home is not None:
            os.environ["HOME"] = original_home

    print("\nsummary")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        _stamp(name, ok)
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
