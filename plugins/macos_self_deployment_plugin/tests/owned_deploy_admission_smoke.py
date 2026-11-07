#!/usr/bin/env python3
"""Owned-deploy admission across the macOS swap, rollback and stop paths (no pytest).

A real :class:`SwapOrchestrator` is held inside ``build_candidate`` (an
existing deploy before its build/executor completes) by a blocking fake
release manager; competitors then run through the plugin's existing
entrypoints. Every runtime root is a private scratch directory reached
through ``XDG_RUNTIME_DIR``; no live router, solet or signal is touched —
the only processes signalled are this smoke's own harmless fixtures.

Pins: ``stop_self``, a second restart and a durable rollback are all refused
before any sentinel/build/spawn while a deploy is held, and a later restart
is admitted once the holder finishes; another thread of this process never
joins; a same-thread nested admission joins and only the outer scope
releases; an exception releases; the active-context lifecycle
(set/correct/clear, stale refusal, orphan retirement, successor
preservation); descriptor transfer keeps exclusion until the child exits;
stop-path spawn failure and unproved identity leave nothing behind; the
watchdog signals only a target whose start token still matches.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/owned_deploy_admission_smoke.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[3]
assert (_ROOT / "ananta" / "src").is_dir(), _ROOT
sys.path.insert(0, str(_ROOT))
for _p in (str(_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"), str(_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ananta.core.actions.owned_deploy_admission import (  # noqa: E402
    ACTIVE_SUFFIX,
    OWNED_DEPLOY_IN_PROGRESS,
    ActiveContext,
    OwnedDeployAdmission,
    OwnedDeployBusyError,
    StaleOwnedDeployContextError,
)
from ananta.core.runtime import get_runtime_dir  # noqa: E402
from ananta.interfaces.lifecycle_result_types import RestartResult  # noqa: E402
from macos_self_deployment_plugin import (  # noqa: E402
    drain_sentinel,
    process_identity,
    stop_self_runner,
    stop_self_watchdog,
)
from macos_self_deployment_plugin.constants import COLOR_BLUE  # noqa: E402
from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin  # noqa: E402
from macos_self_deployment_plugin.preflight_probe_runner import ProbeOutcome  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    GcResult,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import RouterClient  # noqa: E402
from macos_self_deployment_plugin.schema_preflight import (  # noqa: E402
    PreflightVerdict,
    SchemaChange,
)
from macos_self_deployment_plugin.swap_orchestrator import SwapOrchestrator  # noqa: E402

_SOLET = "owned-deploy-smoke"
_HOLD_TIMEOUT_SECONDS = 10.0


class _StatusOnlyRouter:
    def status(self) -> dict[str, Any]:
        return {"active_color": COLOR_BLUE, "active_instance_id": "blue-id", "colors": []}


class _HoldingReleaseManager:
    """``build_candidate`` blocks on the FIRST call until released; counts every call."""

    def __init__(self) -> None:
        self.build_calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def gc(self, *, keep: int | None = None) -> GcResult:
        del keep
        return GcResult(deleted=(), retained=())

    def build_candidate(
        self,
        *,
        manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: object = None,
    ) -> CandidatePaths:
        del manifest_etag, manifest_plugins, schema_snapshot_fn
        self.build_calls += 1
        if self.build_calls == 1:
            self.entered.set()
            self.release.wait(_HOLD_TIMEOUT_SECONDS)
        base = Path("/nonexistent/rel-held")
        return CandidatePaths(
            release_id="rel-held",
            release_dir=base,
            code_root=base / "code",
            venv_python=base / "venv" / "bin" / "python3",
            version_file=base / "VERSION",
            missing_pth_targets=(),
            schema_snapshot=None,
        )

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        raise AssertionError(f"held deploy must never cut over: {candidate.release_id}")

    @property
    def current_release(self) -> str | None:
        return None

    @property
    def previous_release(self) -> str | None:
        return None

    def current_schema_snapshot(self) -> dict[str, object] | None:
        return None

    def candidate_for(self, release_id: str) -> CandidatePaths:
        raise AssertionError(f"forward-path smoke: candidate_for({release_id}) unused")

    def rollback(self) -> SwapResult:
        raise AssertionError("forward-path smoke: rollback unused")


def _refusing_preflight(
    candidate: CandidatePaths,
    *,
    current_snapshot: dict[str, object] | None,
    current_release_exists: bool,
) -> PreflightVerdict:
    """End the held deploy before any spawn once it is released."""
    del candidate, current_snapshot, current_release_exists
    return PreflightVerdict(
        is_additive=False,
        breaking_changes=(SchemaChange(kind="column_removed", namespace="p", table="t", column="c", detail="smoke"),),
    )


def _green_probe(*, candidate: CandidatePaths, app_home: Path) -> ProbeOutcome:
    del app_home
    return ProbeOutcome(ok=True, payload={"ok": True, "release_id": candidate.release_id})


class _Recorder:
    def __init__(self) -> None:
        self.spawns: list[object] = []

    def spawn(self, *args: object, **kwargs: object) -> int:
        del kwargs
        self.spawns.append(args)
        return 4242

    def submit_action_definition(self, action_definition: dict[str, object], context: dict[str, object] | None = None) -> str:
        raise AssertionError(f"held deploy must never enqueue: {action_definition} {context}")


def _orchestrator(manager: _HoldingReleaseManager, recorder: _Recorder) -> SwapOrchestrator:
    return SwapOrchestrator(
        router_client=cast("RouterClient", _StatusOnlyRouter()),
        action_factory=cast("Any", recorder),
        session_factory=lambda: "sess-owned-deploy",
        solet_name=_SOLET,
        release_manager=manager,
        schema_preflight=_refusing_preflight,
        preflight_probe=_green_probe,
        set_color_active=lambda _active: None,
        spawn_fn=recorder.spawn,
        ready_timeout_seconds=2,
        ready_poll_interval_seconds=0.01,
        runtime_dir=get_runtime_dir(_SOLET),
    )


def _admission() -> OwnedDeployAdmission:
    return OwnedDeployAdmission(runtime_dir=get_runtime_dir(_SOLET), solet_name=_SOLET)


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


def _events(attempt_id: str) -> list[str]:
    history, _advisories = _admission().read_history()
    return [entry.event for entry in history if entry.attempt_id == attempt_id]


def _active_path() -> Path:
    return get_runtime_dir(_SOLET) / f"{_SOLET}{ACTIVE_SUFFIX}"


def _write_active(attempt_id: str, owner_pid: int, sequence: int) -> None:
    record = ActiveContext(
        attempt_id=attempt_id,
        solet_name=_SOLET,
        operation="foreign",
        owner_pid=owner_pid,
        owner_start_token=None,
        phase="foreign",
        detail=None,
        sequence=sequence,
        started_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    _active_path().write_text(json.dumps({name: getattr(record, name) for name in record.__slots__}), encoding="utf-8")


def _restart(orchestrator: SwapOrchestrator) -> RestartResult:
    return orchestrator.restart(
        reason="owned-deploy-smoke",
        expected_etag="etag-x",
        dry_run=False,
        app_home=Path("/nonexistent/profile"),
        self_instance_id="blue-id",
        self_color=COLOR_BLUE,
        set_active_targets=[],
    )


class _HeldDeploy:
    """A real orchestrator restart parked inside ``build_candidate`` on a worker thread."""

    def __init__(self) -> None:
        self.manager = _HoldingReleaseManager()
        self.recorder = _Recorder()
        self.results: list[RestartResult] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.results.append(_restart(_orchestrator(self.manager, self.recorder)))

    def __enter__(self) -> _HeldDeploy:
        self._thread.start()
        assert self.manager.entered.wait(_HOLD_TIMEOUT_SECONDS), "held deploy never reached build_candidate"
        return self

    def __exit__(self, *exc: object) -> None:
        self.manager.release.set()
        self._thread.join(_HOLD_TIMEOUT_SECONDS)


def _plugin() -> MacosSelfDeploymentPlugin:
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = _SOLET
    plugin._self_color = COLOR_BLUE
    plugin._self_instance_id = "blue-id"
    return plugin


def test_stop_has_no_effects() -> None:
    """stop_self during a held deploy writes no sentinel and spawns no watchdog."""
    sentinel = drain_sentinel.sentinel_path(_SOLET)
    watchdog_spawns: list[object] = []
    plugin = _plugin()
    plugin.set_watchdog_spawner_for_smoke(lambda request: (watchdog_spawns.append(request), 99999)[1])
    with _HeldDeploy():
        result = plugin.stop_self(reason="stop during held deploy")
        sentinel_writes = 1 if sentinel.exists() else 0
        assert sentinel_writes == 0, f"stop_self wrote the drain sentinel during a held deploy: {result}"
        assert len(watchdog_spawns) == 0, f"stop_self spawned a watchdog during a held deploy: {watchdog_spawns}"
        assert result.status.value == "failed", result
        assert "owned_deploy_in_progress" in result.message, result.message


def test_restart_refused_while_held() -> None:
    """A second restart during a held deploy builds and spawns nothing."""
    manager = _HoldingReleaseManager()
    recorder = _Recorder()
    with _HeldDeploy():
        result = _restart(_orchestrator(manager, recorder))
    assert result.reason_code == OWNED_DEPLOY_IN_PROGRESS, result
    assert result.status.value == "failed", result
    assert manager.build_calls == 0 and recorder.spawns == [], (manager.build_calls, recorder.spawns)


def test_rollback_refused_while_held() -> None:
    """A durable rollback during a held deploy is refused before its CAS or candidate work."""
    manager = _HoldingReleaseManager()
    with _HeldDeploy():
        result = _orchestrator(manager, _Recorder()).rollback_release(
            reason="rollback during held deploy",
            expected_etag="etag-x",
            expected_current_release="rel-current",
            app_home=Path("/nonexistent/profile"),
            self_instance_id="blue-id",
            self_color=COLOR_BLUE,
            set_active_targets=[],
        )
    assert result.reason_code == OWNED_DEPLOY_IN_PROGRESS, result
    assert manager.build_calls == 0, manager.build_calls


def test_same_subject_after_completion_passes() -> None:
    """Part 1 excludes only concurrent owners: the same request is admitted after the holder ends."""
    held = _HeldDeploy()
    with held:
        pass
    assert held.results and held.results[0].reason_code == "schema_preflight_refused", held.results
    again = _restart(_orchestrator(held.manager, held.recorder))
    assert held.manager.build_calls == 2, f"the repeat was not admitted: build_calls={held.manager.build_calls}"
    assert again.reason_code == "schema_preflight_refused", again


def test_unrelated_thread_refused() -> None:
    """Another thread of this process is never the same owner, even with the same pid."""
    with _admission().admit("holder") as lease:
        try:
            with _admission().admit("same-thread"):
                pass
        except OwnedDeployBusyError as exc:
            raise AssertionError(f"same-thread nested admission must join: {exc}") from exc
        assert _contend() == "busy", "another thread joined the holder's attempt"
        assert lease.context.operation == "holder", lease.context


def test_nested_join_releases_at_outer() -> None:
    """Inner exit keeps the lock; only the outermost scope releases."""
    with _admission().admit("outer") as outer:
        with _admission().admit("inner") as inner:
            assert inner.context.attempt_id == outer.context.attempt_id
        assert _contend() == "busy", "inner exit released the outer attempt's lock"
        attempt = outer.context.attempt_id
    assert _contend() == "acquired", "outer exit did not release"
    assert _events(attempt) == ["admitted", "joined", "released"], _events(attempt)


def test_error_releases() -> None:
    """An exception through the scope releases the lock (reacquired from another thread)."""
    attempt = ""
    try:
        with _admission().admit("raising") as lease:
            attempt = lease.context.attempt_id
            raise RuntimeError("planted failure")
    except RuntimeError:
        pass
    assert _contend() == "acquired", "an exception left the lock held"
    assert _events(attempt)[-1] == "failed", _events(attempt)


def test_context_lifecycle() -> None:
    """set/correct phase, set/correct/clear detail, stale refusal, current + historical readback."""
    with _admission().admit("lifecycle") as lease:
        v0 = lease.version
        lease.set_phase("building", expected=v0)
        lease.correct_phase("built", expected=lease.version)
        lease.set_detail("rel-a", expected=lease.version)
        lease.set_detail("rel-b", expected=lease.version)
        final = lease.clear_detail(expected=lease.version)
        assert final.sequence == 5 and final.phase == "built" and final.detail is None, final
        assert _admission().read_active().context == final
        try:
            lease.set_phase("stale", expected=v0)
        except StaleOwnedDeployContextError:
            pass
        else:
            raise AssertionError("a stale expected version was accepted")
        attempt = final.attempt_id
    assert _admission().read_active().context is None, "release did not clear the active record"
    assert _events(attempt) == ["admitted", "set", "corrected", "set", "corrected", "cleared", "released"], _events(attempt)


def test_stale_context_non_authority() -> None:
    """Leftover/corrupt records never fence; a stale owner never clears a successor's record."""
    get_runtime_dir(_SOLET)
    _write_active("deadbeef", 999_999, 3)
    with _admission().admit("after-orphan"):
        pass
    assert _events("deadbeef") == ["orphaned"], _events("deadbeef")
    _active_path().write_text("{not json", encoding="utf-8")
    with _admission().admit("after-corrupt") as lease:
        assert lease.context.operation == "after-corrupt"
    with _admission().admit("stale-owner") as lease:
        _write_active("successor", 4242, 7)
        try:
            lease.set_phase("overwrite", expected=lease.version)
        except StaleOwnedDeployContextError:
            pass
        else:
            raise AssertionError("a stale owner overwrote a successor's record")
        attempt = lease.context.attempt_id
    successor = _admission().read_active().context
    assert successor is not None and successor.attempt_id == "successor", successor
    assert _events(attempt)[-1] == "stale_clear_refused", _events(attempt)


def _lock_only_child(lock_fd: int, children: list[subprocess.Popen[bytes]]) -> int:
    child = subprocess.Popen(  # noqa: S603 — this interpreter, inline lock-holding fixture
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        pass_fds=(lock_fd,),
        stdin=subprocess.PIPE,
    )
    children.append(child)
    return child.pid


def test_transfer_holds_until_child_exit() -> None:
    """Custody moves to the child: exclusion holds after the parent's copy closes, ends at child exit."""
    children: list[subprocess.Popen[bytes]] = []
    with _admission().admit("stop_self") as lease:
        child_pid = lease.transfer(lambda fd: _lock_only_child(fd, children))
        assert lease.transferred
        assert _contend() == "busy", "the transferred lock was released at handoff"
    child = children[0]
    try:
        assert _contend() == "busy", "the parent scope exit released the child's lock"
        active = _admission().read_active().context
        assert active is not None and active.owner_pid == child_pid and active.phase == "transferred", active
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(_HOLD_TIMEOUT_SECONDS)
    assert _contend() == "acquired", "child exit did not free the lock"
    assert _events(active.attempt_id)[-2:] == ["transferred", "orphaned"], _events(active.attempt_id)


def test_stop_spawn_failure_releases() -> None:
    """A failed watchdog launch removes only this attempt's sentinel and frees the lock."""
    sentinel = drain_sentinel.sentinel_path(_SOLET)

    def _failing(request: stop_self_watchdog.WatchdogRequest) -> int:
        raise OSError(f"planted spawn failure for {request.target_pid}")

    result = stop_self_runner.run(solet_name=_SOLET, reason="spawn fails", dry_run=False, watchdog_spawner=_failing)
    assert result.status.value == "failed" and "watchdog spawn failed" in result.message, result
    assert not sentinel.exists(), "a failed spawn left this attempt's sentinel behind"
    assert _contend() == "acquired", "a failed spawn left the lock held"
    sentinel.touch()
    spawned: list[object] = []
    again = _plugin()
    again.set_watchdog_spawner_for_smoke(lambda request: (spawned.append(request), 1)[1])
    stopped = again.stop_self(reason="already stopped")
    assert stopped.status.value == "already_stopped" and sentinel.exists() and spawned == [], (stopped, spawned)


def test_stop_unproved_identity() -> None:
    """Without a provable start token for this process, nothing is written or spawned."""
    spawned: list[object] = []
    result = stop_self_runner.run(
        solet_name=_SOLET,
        reason="no identity",
        dry_run=False,
        watchdog_spawner=lambda request: (spawned.append(request), 1)[1],
        identity_probe=lambda _pid: None,
    )
    assert result.status.value == "failed" and "identity" in result.message, result
    assert not drain_sentinel.sentinel_path(_SOLET).exists() and spawned == [], spawned


def test_watchdog_identity() -> None:
    """Signals go only to a target whose live start token still matches; reuse sends nothing."""
    kills: list[tuple[int, int]] = []
    sleeps: list[float] = []

    def _kill(pid: int, signum: int) -> None:
        kills.append((pid, signum))

    reused = stop_self_watchdog.run_signal_sequence(
        target_pid=77,
        start_token="t0",
        identity_probe=lambda _pid: "t1",
        kill=_kill,
        sleep=sleeps.append,
    )
    assert reused == stop_self_watchdog.OUTCOME_IDENTITY_UNPROVED and kills == [], kills
    proved = stop_self_watchdog.run_signal_sequence(
        target_pid=77,
        start_token="t0",
        identity_probe=lambda _pid: "t0",
        kill=_kill,
        sleep=sleeps.append,
    )
    assert proved == stop_self_watchdog.OUTCOME_SIGNALLED, proved
    assert kills == [(77, signal.SIGTERM), (77, signal.SIGKILL)], kills

    def _gone(pid: int, signum: int) -> None:
        raise ProcessLookupError(pid, signum)

    gone = stop_self_watchdog.run_signal_sequence(
        target_pid=77,
        start_token="t0",
        identity_probe=lambda _pid: "t0",
        kill=_gone,
        sleep=sleeps.append,
    )
    assert gone == stop_self_watchdog.OUTCOME_TARGET_GONE, gone


def test_watchdog_custody_end_to_end() -> None:
    """The production watchdog holds the lock through its delayed signals and frees it on exit."""
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"), str(_ROOT / "ananta" / "src"), os.environ.get("PYTHONPATH", "")],
    )
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603 — harmless fixture target
    try:
        token = process_identity.start_token(target.pid)
        assert token is not None, "could not read the fixture target's start token"
        with _admission().admit("stop_self") as lease:
            lease.transfer(lambda fd: stop_self_watchdog.spawn(stop_self_watchdog.WatchdogRequest(target.pid, token, fd)))
        assert _contend() == "busy", "the watchdog did not hold the transferred lock"
        assert target.wait(_HOLD_TIMEOUT_SECONDS) == -signal.SIGTERM, target.returncode
        assert _contend() == "busy", "custody ended before the watchdog's bounded window"
    finally:
        if target.poll() is None:
            target.kill()
            target.wait(_HOLD_TIMEOUT_SECONDS)
    deadline = time.monotonic() + 30.0
    while _contend() != "acquired":
        assert time.monotonic() < deadline, "the watchdog never exited and released the lock"
        time.sleep(0.25)


_TESTS: tuple[Callable[[], None], ...] = (
    test_stop_has_no_effects,
    test_restart_refused_while_held,
    test_rollback_refused_while_held,
    test_same_subject_after_completion_passes,
    test_unrelated_thread_refused,
    test_nested_join_releases_at_outer,
    test_error_releases,
    test_context_lifecycle,
    test_stale_context_non_authority,
    test_transfer_holds_until_child_exit,
    test_stop_spawn_failure_releases,
    test_stop_unproved_identity,
    test_watchdog_identity,
    test_watchdog_custody_end_to_end,
)


def run_smoke() -> int:
    print("=== owned_deploy_admission_smoke ===")
    failed: list[str] = []
    for test in _TESTS:
        with tempfile.TemporaryDirectory(prefix="owned_deploy_") as tmp:
            os.environ["XDG_RUNTIME_DIR"] = tmp
            try:
                test()
            except Exception:  # any failure is this test's FAIL; the remaining tests still run
                failed.append(test.__name__)
                print(f"  FAIL  {test.__name__}")
                traceback.print_exc()
            else:
                print(f"  PASS  {test.__name__}")
    print(f"\nowned_deploy_admission_smoke: {len(_TESTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
