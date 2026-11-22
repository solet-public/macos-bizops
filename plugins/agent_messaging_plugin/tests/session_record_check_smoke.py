#!/usr/bin/env python3
"""Start-time managed-session record check: C0-C8."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402
from small_plugin_job_support import Ledger  # noqa: E402

from agent_messaging_plugin import host_end_notice, session_end  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_TRANSITION,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)

DENIED = "error connecting to /tmp/tmux-501/default (Permission denied)"


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _insert(
    state: StateManagementInterface,
    key: str,
    *,
    host: str = "tmux",
    ref: str | None = None,
    lifecycle: str = LIFECYCLE_LIVE,
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=key,
            label="record-check-fixture",
            session_origin="registered" if host == "operator" else "spawned",
            host=host,
        ),
    )
    if ref is not None:
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": key}},
            {"host_ref": ref},
        )
    transition_lifecycle_state(
        state,
        agent_instance_id=key,
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="fixture",
    )
    if lifecycle != LIFECYCLE_LIVE:
        transition_lifecycle_state(
            state,
            agent_instance_id=key,
            from_state=LIFECYCLE_LIVE,
            to_state=lifecycle,
            directed_by="fixture",
        )


def _fixture() -> StateManagementInterface:
    state = _state()
    _insert(state, "gone-overdue", ref="gone-1", lifecycle=LIFECYCLE_OVERDUE)
    _insert(state, "gone-idle", ref="gone-1", lifecycle=LIFECYCLE_IDLE)
    _insert(state, "present", ref="present-1")
    _insert(state, "permission", ref="perm-1")
    _insert(state, "operator-a", host="operator")
    _insert(state, "operator-b", host="operator")
    _insert(state, "headless-gone", host="headless", ref="dead-process")
    _insert(state, "already-ended", ref="gone-2")
    transition_lifecycle_state(
        state,
        agent_instance_id="already-ended",
        from_state=LIFECYCLE_LIVE,
        to_state=LIFECYCLE_TERMINATED,
        directed_by="fixture",
    )
    return state


class _FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> Any:
        self.calls.append(list(argv))
        ref = argv[-1]
        if ref == "=present-1":
            return SimpleNamespace(returncode=0, stderr="")
        if ref == "=perm-1":
            return SimpleNamespace(returncode=1, stderr=DENIED)
        return SimpleNamespace(returncode=1, stderr=f"can't find session: {ref}")


class _Headless:
    def alive(self, _ref: str) -> bool:
        return False


class _Orchestrator:
    def __init__(self, state: StateManagementInterface) -> None:
        self.state = state

    def get_service(self, name: str) -> StateManagementInterface | None:
        return self.state if name == "state_service" else None


class _Jobs:
    def __init__(self, *, error: bool = False, raises: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error = error
        self.raises = raises
        self.outcome: dict[str, Any] | None = None

    def submit(self, plugin: Any, action: str, parameters: dict[str, Any], state: dict[str, Any], operation: Any) -> dict[str, Any]:
        self.calls.append((plugin, action, parameters, state))
        if self.raises:
            raise RuntimeError("submit failed")
        if self.error:
            return {"action_status": "failed", "error": {"code": "submit_failed"}}
        self.outcome = operation(parameters)
        return {"action_status": "completed", "data": {"job_id": "job-fixture", "status": "queued"}, "error": None}


class SessionRecordCheckSmoke(unittest.TestCase):
    def _check(self, state: StateManagementInterface, fake: _FakeRun) -> dict[str, Any]:
        with mock.patch.object(host_end_notice, "_run", fake), mock.patch.object(session_end, "resolve_host_driver", return_value=(_Headless(), "headless")):
            return session_end.check_session_records(state, directed_by="fixture", tmux=["/fake/tmux"])

    def test_c0_modules_load_from_worktree(self) -> None:
        from agent_messaging_plugin import plugin  # noqa: PLC0415

        for module in (session_end, plugin):
            self.assertTrue(Path(str(module.__file__)).resolve().is_relative_to(ROOT))

    def test_c1_counts_and_unknown_details(self) -> None:
        result = self._check(_fixture(), _FakeRun())
        self.assertEqual(result["counts"], {"checked": 7, "ended": 3, "present": 1, "unknown": 3, "changed_meanwhile": 0})
        self.assertEqual(result["unknown_by_detail"], {"no host reference recorded": 2, DENIED: 1})
        self.assertNotIn("already-ended", result["ended"])

    def test_c2_state_and_reason(self) -> None:
        state = _fixture()
        before = {key: read_managed_session(state, key) for key in ("present", "permission", "operator-a", "operator-b", "already-ended")}
        self._check(state, _FakeRun())
        for key in ("gone-overdue", "gone-idle", "headless-gone"):
            row = read_managed_session(state, key)
            self.assertEqual(row["lifecycle_state"], LIFECYCLE_TERMINATED)
            self.assertEqual(row["host_liveness"], "dead")
            transitions = require_records(state.query_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_SESSION_TRANSITION, "filters": {"agent_instance_id": key, "to_state": LIFECYCLE_TERMINATED}}))
            self.assertTrue(str(transitions[-1]["reason"]).startswith("ended: the solet's start check found its host session gone"))
        for key, prior in before.items():
            self.assertEqual(read_managed_session(state, key), prior)

    def test_c3_shared_reference_checked_once(self) -> None:
        fake = _FakeRun()
        self._check(_fixture(), fake)
        self.assertEqual(sum(call[-1] == "=gone-1" for call in fake.calls), 1)
        self.assertFalse(any(call[-1] == "=gone-2" for call in fake.calls))

    def test_c4_second_check_is_idempotent(self) -> None:
        state = _fixture()
        fake = _FakeRun()
        self._check(state, fake)
        second = self._check(state, fake)
        self.assertEqual(second["counts"], {"checked": 4, "ended": 0, "present": 1, "unknown": 3, "changed_meanwhile": 0})

    def test_c5_start_submits_one_job(self) -> None:
        plugin = AgentMessagingPlugin()
        plugin.orchestrator_ref = _Orchestrator(_fixture())
        plugin._service_started_at = "fixture-start"  # noqa: SLF001
        plugin._start_record_check_pending = True  # noqa: SLF001
        jobs = _Jobs()
        plugin._operation_jobs = cast("Any", jobs)  # noqa: SLF001
        with mock.patch.object(host_end_notice, "_run", _FakeRun()), mock.patch.object(session_end, "resolve_host_driver", return_value=(_Headless(), "headless")):
            plugin._submit_start_record_check()  # noqa: SLF001
        self.assertEqual(len(jobs.calls), 1)
        self.assertEqual(jobs.calls[0][1:3], ("check_session_records_at_start", {}))
        self.assertEqual(jobs.calls[0][3], {"session_id": "solet-start", "flow_id": "solet-start-fixture-start"})
        self.assertEqual(jobs.outcome["action_status"], "completed")  # type: ignore[index]
        self.assertIn("counts", jobs.outcome["data"])  # type: ignore[index]

    def test_c6_failures_log_and_start_keeps_result(self) -> None:
        plugin = AgentMessagingPlugin()
        for jobs in (_Jobs(error=True), _Jobs(raises=True)):
            plugin._operation_jobs = cast("Any", jobs)  # noqa: SLF001
            plugin._start_record_check_pending = True  # noqa: SLF001
            with self.assertLogs("agent_messaging_plugin.plugin", level="ERROR") as logged:
                plugin._submit_start_record_check()  # noqa: SLF001
            self.assertEqual(len(logged.records), 1)
        with mock.patch.object(plugin._launch_jobs, "start"), mock.patch.object(plugin, "_start_choreography_worker"), mock.patch.object(plugin, "_submit_start_record_check") as submit:
            result = plugin._f5_start_services()  # noqa: SLF001
        submit.assert_not_called()
        self.assertEqual(result.get("action_status"), "completed")

    @staticmethod
    def _counting_acquire(real_acquire: Any, calls: list[Any]) -> Any:
        def counting_acquire() -> Any:
            calls.append(real_acquire())
            return calls[-1]

        return counting_acquire

    def _wait_for_attempts(self, calls: list[Any], target: int, failure: str) -> None:
        for _ in range(1000):
            if len(calls) >= target:
                return
            threading.Event().wait(0.01)
        self.fail(failure)

    @staticmethod
    def _job_settled(ledger: Ledger, plugin: AgentMessagingPlugin) -> bool:
        no_queued = all(row["status"] != "queued" for row in ledger.rows.values())
        return bool(ledger.rows) and no_queued and plugin._operation_jobs._thread is None  # noqa: SLF001

    def _wait_for_job_settled(self, ledger: Ledger, plugin: AgentMessagingPlugin) -> None:
        for _ in range(200):
            if self._job_settled(ledger, plugin):
                return
            threading.Event().wait(0.01)

    def _let_manager_appear(self, plugin: AgentMessagingPlugin, orchestrator: _Orchestrator, ledger: Ledger, calls: list[Any]) -> None:
        self._wait_for_attempts(calls, 5, "the choreography worker made fewer than 5 manager acquisition attempts in 10 s")
        self.assertEqual(ledger.rows, {})  # no manager yet: nothing queued, nothing logged
        orchestrator.async_job_manager = ledger  # type: ignore[attr-defined]
        self._wait_for_attempts(calls, len(calls) + 5, "the choreography worker made fewer than 5 more manager acquisition attempts in 10 s after the manager appeared")
        self._wait_for_job_settled(ledger, plugin)

    def test_c8_start_check_queued_once_after_manager_appears(self) -> None:
        orchestrator = _Orchestrator(_fixture())
        plugin = AgentMessagingPlugin()
        plugin.orchestrator_ref = orchestrator
        ledger = Ledger()
        calls: list[Any] = []
        counting_acquire = self._counting_acquire(plugin._try_acquire_async_job_manager, calls)  # noqa: SLF001
        with (
            mock.patch.object(plugin._launch_jobs, "start"),
            mock.patch.object(plugin, "_try_acquire_async_job_manager", side_effect=counting_acquire),
            mock.patch.object(plugin, "_poll_and_process_one_choreography_job"),
            mock.patch.object(AgentMessagingPlugin, "_CHOREOGRAPHY_POLL_INTERVAL_SECONDS", 0.01),
            mock.patch.object(host_end_notice, "_run", _FakeRun()),
            mock.patch.object(session_end, "resolve_host_driver", return_value=(_Headless(), "headless")),
            self.assertNoLogs("agent_messaging_plugin.plugin", level="ERROR"),
        ):
            self.assertEqual(plugin._f5_start_services().get("action_status"), "completed")  # noqa: SLF001
            try:
                self._let_manager_appear(plugin, orchestrator, ledger, calls)
            finally:
                plugin._stop_choreography_worker()  # noqa: SLF001
        self.assertEqual(list(ledger.rows), ["job-1"])
        row = ledger.rows["job-1"]
        self.assertEqual(row["provider_name"], f"{plugin.name}.check_session_records_at_start")
        self.assertEqual(row["status"], "completed")
        self.assertIn("counts", row["result"]["data"])

    def test_c7_real_private_tmux_server(self) -> None:
        tmux = shutil.which("tmux")
        self.assertIsNotNone(tmux, "tmux missing on lane host")
        prefix = [str(tmux), "-L", "engine6-check-" + uuid4().hex[:12]]
        socket_path: Path | None = None
        try:
            started = subprocess.run([*prefix, "new-session", "-d", "-s", "live-1", "sleep 300"], capture_output=True, text=True, check=False)
            self.assertEqual(started.returncode, 0, started.stderr)
            socket = subprocess.run([*prefix, "display-message", "-p", "-t", "live-1", "#{socket_path}"], capture_output=True, text=True, check=False)
            self.assertEqual(socket.returncode, 0, socket.stderr)
            socket_path = Path(socket.stdout.strip())
            state = _state()
            _insert(state, "live", ref="live-1")
            _insert(state, "dead", ref="dead-1")
            result = session_end.check_session_records(state, directed_by="fixture", tmux=prefix)
            self.assertEqual(result["ended"], ["dead"])
            self.assertEqual(result["present"], ["live"])
        finally:
            subprocess.run([*prefix, "kill-server"], capture_output=True, text=True, check=False)
            if socket_path is not None:
                socket_path.unlink(missing_ok=True)
                self.assertFalse(socket_path.exists())


if __name__ == "__main__":
    unittest.main()
