"""Owner-probe routines for session-launch reservations, called by session_launch_failure_smoke.

The caller puts the test and plugin source paths on sys.path before importing this module.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, cast
from unittest.mock import patch

from async_job_fixture_support import JobState
from session_launch_fixture_support import dispatch, effect_patch, finish_fixture, plugin_fixture

import agent_messaging_plugin.session_launch_jobs as launch_jobs_module
import agent_messaging_plugin.session_launch_store as launch_store_module
from agent_messaging_plugin.session_launch_store import release_launch, reserve_launch


def _owner_job(manager: Any, status: str) -> str:
    job_id = manager.create_job("agent_messaging_plugin", "spawn_session", {"notes": f"{status} owner"})["data"]["job_id"]
    if status != "queued":
        manager.update_job(job_id, {"status": status, "result": {}} if status == "completed" else {"status": status})
    return str(job_id)


def _error_code(receipt: dict[str, Any]) -> object:
    return (receipt.get("error") or {}).get("code")


def _launch_with_advisories(plugin: Any) -> tuple[dict[str, Any], list[str]]:
    """Second launch through the real entry point, capturing advisory lines."""
    effect = effect_patch("spawn_session", plugin, lambda *_args, **_kwargs: {"agent_instance_id": "agi-next"})
    with effect, patch.object(launch_jobs_module.logger, "warning") as advisory:
        receipt = dispatch("spawn_session", plugin)
        if receipt["action_status"] == "completed":
            plugin._launch_jobs.future.result(timeout=5)
    return receipt, [call.args[0] % call.args[1:] for call in advisory.call_args_list]


def _ps_start_reads(run: Any, pid: int) -> int:
    """Host start-token reads (``ps -o lstart= -p <pid>``) for one pid through a patched subprocess.run."""
    target = ["ps", "-o", "lstart=", "-p", str(pid)]
    return sum(1 for call in run.call_args_list if call.args and call.args[0] == target)


def null_pid_terminal_owner_released() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    job_id = _owner_job(plugin._try_acquire_async_job_manager(), "completed")
    state.rows["session_launch_reservation"] = [
        {"id": "slr-null-pid", "resource_key": "session_launch", "operation_id": "null-op", "job_id": job_id, "owner_pid": None, "owner_started": None},
    ]
    receipt, advisories = _launch_with_advisories(plugin)
    assert receipt["action_status"] == "completed", "null_pid_terminal_owner_must_release"
    assert f"Launch reservation released: operation=null-op job={job_id} reason=terminal_job" in advisories
    assert state.rows["session_launch_reservation"] == []
    finish_fixture(plugin)
    print("PASS null_pid_terminal_owner_released")


def own_start_token_read_once() -> None:
    state = JobState()
    pid = os.getpid()
    with patch.object(launch_store_module.subprocess, "run", wraps=subprocess.run) as run:
        for operation in ("own-op-1", "own-op-2"):
            reserve_launch(cast(Any, state), operation)
            release_launch(cast(Any, state), operation, pid)
    assert _ps_start_reads(run, pid) <= 1, "own_start_token_read_once_per_process"
    with patch.dict(launch_store_module._OWN_START_TOKENS):
        launch_store_module._OWN_START_TOKENS.clear()
        with patch.object(launch_store_module, "process_start_token", side_effect=["", "fixture-start"]) as token_read:
            reserve_launch(cast(Any, state), "empty-op-1")
            release_launch(cast(Any, state), "empty-op-1", pid)
            reserve_launch(cast(Any, state), "empty-op-2")
            started = state.rows["session_launch_reservation"][0]["owner_started"]
        assert token_read.call_count == 2 and started == "fixture-start", "own_start_token_empty_not_cached"
    print("PASS own_start_token_read_once")


def conflict_probes_owner_once() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    job_id = _owner_job(plugin._try_acquire_async_job_manager(), "processing")
    parent = os.getppid()
    parent_started = launch_store_module.process_start_token(parent)
    assert parent_started, "live_parent_start_token_unreadable"
    state.rows["session_launch_reservation"] = [
        {"id": "slr-parent", "resource_key": "session_launch", "operation_id": "parent-op", "job_id": job_id, "owner_pid": parent, "owner_started": parent_started},
    ]
    with patch.object(launch_store_module.subprocess, "run", wraps=subprocess.run) as run:
        busy, advisories = _launch_with_advisories(plugin)
    assert _error_code(busy) == "launch_busy" and busy["data"]["job_id"] == job_id, "conflict_probes_owner_once_live_owner_refuses"
    assert _ps_start_reads(run, parent) == 1, "conflict_probes_owner_once"
    assert advisories == [], "live_owner_probe_keeps_no_advisory"
    assert state.rows["session_launch_reservation"][0]["operation_id"] == "parent-op"
    finish_fixture(plugin)
    print("PASS conflict_probes_owner_once")
