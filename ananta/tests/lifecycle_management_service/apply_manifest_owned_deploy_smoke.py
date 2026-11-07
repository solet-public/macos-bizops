#!/usr/bin/env python3
"""Owned-deploy admission at the core ``apply_manifest`` commit (no pytest).

Drives the REAL ``LifecycleManagementService.apply_manifest`` against a
real temp ``APP_HOME``; the only fake is the bound deployment plugin (the
seam the macOS and cloud plugins fill). The runtime root is redirected
with ``XDG_RUNTIME_DIR`` so the admission lock never touches a live one.

Pins: a competing commit during a held deploy writes and delegates nothing
(``test_apply_keeps_bytes``); with no owner a first and a different
manifest both apply, and a cloud-style binding (no admission of its own)
is delegated exactly once; a plugin that admits on the same thread joins
the commit's attempt and the lock stays held until the OUTER commit ends;
normal, error and crash exits all leave the lock free.

Run:
    SOLET_NAME=<name> .venv/bin/python3 ananta/tests/lifecycle_management_service/apply_manifest_owned_deploy_smoke.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
assert (_ROOT / "ananta" / "src").is_dir(), _ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _probe_fixture_support import (  # noqa: E402
    FIXTURE_PLUGIN_NAME,
    GOOD_SOURCE,
    write_fixture,
)
from ananta.core.actions.owned_deploy_admission import (  # noqa: E402
    OwnedDeployAdmission,
    OwnedDeployBusyError,
)
from ananta.core.config.environment_config import EnvironmentConfig  # noqa: E402
from ananta.interfaces.lifecycle_result_types import (  # noqa: E402
    RestartResult,
    RestartStatus,
)
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)

_HOLD_TIMEOUT_SECONDS = 10.0


def _admission() -> OwnedDeployAdmission:
    return OwnedDeployAdmission.for_solet(EnvironmentConfig.solet_name())


def _contend() -> str:
    """Try the solet's admission from a fresh thread: ``acquired`` or ``busy``."""
    outcome: list[str] = []

    def _attempt() -> None:
        try:
            with _admission().admit("competitor"):
                outcome.append("acquired")
        except OwnedDeployBusyError:
            outcome.append("busy")

    worker = threading.Thread(target=_attempt)
    worker.start()
    worker.join(_HOLD_TIMEOUT_SECONDS)
    return outcome[0] if outcome else "hung"


def _queued(reason: str, expected_etag: str, call: int) -> RestartResult:
    return RestartResult(
        status=RestartStatus.QUEUED,
        restart_action_id=f"ae-smoke-{call}",
        message="queued (smoke)",
        reason=reason,
        expected_etag=expected_etag,
        dry_run=False,
    )


class _CloudStylePlugin:
    """A binding that never admits itself (the cloud plugin's shape)."""

    def __init__(self, status: RestartStatus = RestartStatus.QUEUED) -> None:
        self.calls = 0
        self.status = status

    def restart_with_manifest(
        self,
        *,
        new_manifest: dict[str, Any],
        expected_etag: str,
        reason: str,
        dry_run: bool,
    ) -> RestartResult:
        del new_manifest, dry_run
        self.calls += 1
        if self.status is not RestartStatus.QUEUED:
            return RestartResult(
                status=self.status,
                restart_action_id="",
                message="failed (smoke)",
                reason=reason,
                expected_etag=expected_etag,
                dry_run=False,
            )
        return _queued(reason, expected_etag, self.calls)


class _NestedAdmittingPlugin:
    """A binding that admits on the caller's thread, as the macOS orchestrator does."""

    def __init__(self) -> None:
        self.inside_inner: str = ""
        self.after_inner: str = ""
        self.joined_attempts: list[str] = []
        self.outer_attempts: list[str] = []

    def restart_with_manifest(
        self,
        *,
        new_manifest: dict[str, Any],
        expected_etag: str,
        reason: str,
        dry_run: bool,
    ) -> RestartResult:
        del new_manifest, dry_run
        outer = _admission().read_active().context
        self.outer_attempts.append(outer.attempt_id if outer is not None else "")
        with _admission().admit("swap_restart") as inner:
            self.joined_attempts.append(inner.context.attempt_id)
            self.inside_inner = _contend()
        self.after_inner = _contend()
        return _queued(reason, expected_etag, 1)


class _HoldingDeploymentPlugin:
    """Bound ``self_deployment_service`` whose FIRST restart blocks until released.

    The first ``apply_manifest`` therefore stays inside its delegate (an
    existing deploy held before its executor completes) while a competitor
    runs. Later calls return immediately.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def restart_with_manifest(
        self,
        *,
        new_manifest: dict[str, Any],
        expected_etag: str,
        reason: str,
        dry_run: bool,
    ) -> RestartResult:
        del new_manifest, dry_run
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            self.release.wait(_HOLD_TIMEOUT_SECONDS)
        return _queued(reason, expected_etag, self.calls)


class _InterruptingPlugin:
    """A delegate that raises through the commit (a crash-shaped exit, not an error envelope)."""

    def restart_with_manifest(self, **_kwargs: object) -> RestartResult:
        raise KeyboardInterrupt


_Plugin = _HoldingDeploymentPlugin | _CloudStylePlugin | _NestedAdmittingPlugin | _InterruptingPlugin


class _FakeOrchestratorRef:
    def __init__(self, app_home: Path, plugin: _Plugin) -> None:
        self.APP_HOME = str(app_home)
        self._plugin = plugin

    def get_service(self, name: str) -> _Plugin | None:
        return self._plugin if name == "self_deployment_service" else None


def _seed_app_home(app_home: Path) -> None:
    config = app_home / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "manifest.yaml").write_bytes(
        f"profile_name: local\nplugins:\n- {FIXTURE_PLUGIN_NAME}\n".encode(),
    )
    (config / "service_bindings.json").write_bytes(
        ('{"self_deployment_service": "' + FIXTURE_PLUGIN_NAME + '"}\n').encode(),
    )


def _apply(service: LifecycleManagementService, profile_name: str) -> dict[str, Any]:
    return service.apply_manifest(
        new_manifest={
            "plugins": [FIXTURE_PLUGIN_NAME],
            "service_bindings": {},
            "profile_name": profile_name,
        },
        reason=f"owned-deploy-smoke {profile_name}",
    )


def _service(app_home: Path, plugin: _Plugin) -> LifecycleManagementService:
    return LifecycleManagementService(_FakeOrchestratorRef(app_home, plugin))


def _hold_first_apply(
    service: LifecycleManagementService,
    plugin: _HoldingDeploymentPlugin,
) -> tuple[threading.Thread, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    holder = threading.Thread(
        target=lambda: results.append(_apply(service, "first")),
        daemon=True,
    )
    holder.start()
    assert plugin.entered.wait(_HOLD_TIMEOUT_SECONDS), "first apply never reached its delegate"
    return holder, results


def _untouched(path: Path) -> tuple[bytes, int]:
    """Exact bytes plus inode: every manifest write (and restore) is temp+rename, so a
    transient write that a rollback later put back still shows as a new inode."""
    return path.read_bytes(), path.stat().st_ino


def test_apply_keeps_bytes(app_home: Path) -> None:
    """A competing commit during a held deploy writes nothing and delegates nothing."""
    _seed_app_home(app_home)
    plugin = _HoldingDeploymentPlugin()
    service = _service(app_home, plugin)
    holder, _results = _hold_first_apply(service, plugin)
    manifest = app_home / "config" / "manifest.yaml"
    bindings = app_home / "config" / "service_bindings.json"
    original_manifest = _untouched(manifest)
    original_bindings = _untouched(bindings)
    try:
        competing = _apply(_service(app_home, plugin), "competitor")
        assert _untouched(manifest) == original_manifest, "competing commit rewrote the manifest"
        assert _untouched(bindings) == original_bindings, "competing commit rewrote the bindings"
        assert plugin.calls == 1, f"competing commit delegated a restart: calls={plugin.calls}"
        data = competing["data"]
        assert data["status"] == "rejected", data
        assert any(r.startswith("owned_deploy_in_progress") for r in data["rejection_reasons"]), data
    finally:
        plugin.release.set()
        holder.join(_HOLD_TIMEOUT_SECONDS)


def test_first_and_different_manifest_pass(app_home: Path) -> None:
    """With no owner, a first and then a different manifest both apply, one delegation each."""
    _seed_app_home(app_home)
    plugin = _CloudStylePlugin()
    service = _service(app_home, plugin)
    first = _apply(service, "first")["data"]
    assert first["status"] == "applied", first
    second = _apply(service, "different")["data"]
    assert second["status"] == "applied", second
    assert b"profile_name: different" in (app_home / "config" / "manifest.yaml").read_bytes()
    assert plugin.calls == 2, f"each unowned apply delegates once: calls={plugin.calls}"
    assert _contend() == "acquired", "a finished apply left the lock held"


def test_cloud_delegates_once(app_home: Path) -> None:
    """A cloud-style binding (never admits itself) is delegated exactly once when unowned."""
    _seed_app_home(app_home)
    plugin = _CloudStylePlugin()
    data = _apply(_service(app_home, plugin), "cloud")["data"]
    assert data["status"] == "applied", data
    assert plugin.calls == 1, f"cloud binding delegated {plugin.calls} times"
    assert data["restart_status"] == "queued", data


def test_nested_delegate_joins(app_home: Path) -> None:
    """A same-thread plugin admission joins the commit; only the outer scope releases."""
    _seed_app_home(app_home)
    plugin = _NestedAdmittingPlugin()
    data = _apply(_service(app_home, plugin), "nested")["data"]
    assert data["status"] == "applied", data
    assert plugin.joined_attempts == plugin.outer_attempts, (plugin.joined_attempts, plugin.outer_attempts)
    assert plugin.inside_inner == "busy", f"competitor inside the joined scope: {plugin.inside_inner}"
    assert plugin.after_inner == "busy", f"inner exit released the outer commit's lock: {plugin.after_inner}"
    assert _contend() == "acquired", "the outer commit did not release on return"
    history, advisories = _admission().read_history()
    events = [entry.event for entry in history if entry.attempt_id == plugin.outer_attempts[0]]
    assert events == ["admitted", "joined", "released"], (events, advisories)


def test_error_releases(app_home: Path) -> None:
    """A failed restart (rollback envelope) and a raising delegate both leave the lock free."""
    _seed_app_home(app_home)
    failed = _apply(_service(app_home, _CloudStylePlugin(RestartStatus.FAILED)), "failing")["data"]
    assert failed["status"] == "restart_failed_after_manifest_commit", failed
    assert _contend() == "acquired", "an error envelope left the lock held"
    try:
        _apply(_service(app_home, _InterruptingPlugin()), "interrupted")
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("the raising delegate did not propagate")
    assert _contend() == "acquired", "an exception through the commit left the lock held"


_CRASH_CHILD = """
import sys, time
sys.path.insert(0, sys.argv[1])
from ananta.core.actions.owned_deploy_admission import OwnedDeployAdmission
from ananta.core.config.environment_config import EnvironmentConfig
with OwnedDeployAdmission.for_solet(EnvironmentConfig.solet_name()).admit("crash_fixture"):
    print("held", flush=True)
    time.sleep(60)
"""


def test_crash_releases(app_home: Path) -> None:
    """A holder killed with SIGKILL frees the lock; its leftover record never fences."""
    _seed_app_home(app_home)
    child = subprocess.Popen(  # noqa: S603 — this interpreter + an inline lock-only fixture
        [sys.executable, "-c", _CRASH_CHILD, str(_ROOT / "ananta" / "src")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "held"
        assert _contend() == "busy", "a live holder in another process was not exclusive"
        busy = _apply(_service(app_home, _CloudStylePlugin()), "while-held")["data"]
        assert busy["status"] == "rejected", busy
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(_HOLD_TIMEOUT_SECONDS)
    plugin = _CloudStylePlugin()
    data = _apply(_service(app_home, plugin), "after-crash")["data"]
    assert data["status"] == "applied", data
    history, _advisories = _admission().read_history()
    assert any(entry.event == "orphaned" and entry.operation == "crash_fixture" for entry in history), history


_TESTS: tuple[Callable[[Path], None], ...] = (
    test_apply_keeps_bytes,
    test_first_and_different_manifest_pass,
    test_cloud_delegates_once,
    test_nested_delegate_joins,
    test_error_releases,
    test_crash_releases,
)


def run_smoke() -> int:
    print("=== apply_manifest_owned_deploy_smoke ===")
    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_RUNTIME_DIR"] = str(Path(tmp) / "xdg")
        fixture_dir = Path(tmp) / "fixture"
        write_fixture(fixture_dir, GOOD_SOURCE)
        sys.path.insert(0, str(fixture_dir))
        try:
            for test in _TESTS:
                app_home = Path(tempfile.mkdtemp(dir=tmp, prefix="app_home_"))
                try:
                    test(app_home)
                except Exception:  # any failure is this test's FAIL; the remaining tests still run
                    failed.append(test.__name__)
                    print(f"  FAIL  {test.__name__}")
                    traceback.print_exc()
                else:
                    print(f"  PASS  {test.__name__}")
        finally:
            sys.path.remove(str(fixture_dir))
    print(f"\napply_manifest_owned_deploy_smoke: {len(_TESTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
