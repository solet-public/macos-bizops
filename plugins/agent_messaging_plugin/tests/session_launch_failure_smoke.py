#!/usr/bin/env python3
"""Launch admission, truthful stage failures, restart ownership, and shutdown."""

from __future__ import annotations

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "ananta/src", ROOT / "plugins/agent_messaging_plugin/src", ROOT / "ananta/tests/core/actions", Path(__file__).parent):
    sys.path.insert(0, str(path))

from async_job_fixture_support import JobState  # noqa: E402
from session_launch_fixture_support import (  # noqa: E402
    authority_patches,
    blocked_restart_owner,
    dispatch,
    effect_patch,
    finish_fixture,
    owned_restart_request_failure,
    parameters,
    plugin_fixture,
    restart_busy_handoff,
    restart_request_failure,
    terminal_statuses,
)
from session_launch_owner_probe_support import (  # noqa: E402
    _error_code,
    _launch_with_advisories,
    _owner_job,
    conflict_probes_owner_once,
    null_pid_terminal_owner_released,
    own_start_token_read_once,
)

from agent_messaging_plugin.managed_dispatch import DispatchError  # noqa: E402
from agent_messaging_plugin.session_launch_jobs import run_reserved_restart  # noqa: E402
from agent_messaging_plugin.session_launch_store import release_launch, reserve_launch  # noqa: E402


def refusal_and_stage_results() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    denied = plugin.spawn_session_action(parameters("spawn_session"), {})
    assert denied["action_status"] != "completed" and not state.rows.get("job")
    bad = dispatch("spawn_session", plugin, {**parameters("spawn_session"), "role_class": "invalid"})
    assert bad["error"]["code"] == "unknown_role_class" and not state.rows.get("job")
    with authority_patches():
        missing = dispatch("dispatch_managed_work", plugin, {**parameters("dispatch_managed_work"), "unit_id": ""})
        assert missing["error"]["code"] == "unit_id_required" and not state.rows.get("job")
    for stage in ("worktree_failed", "host_start_failed", "first_turn_failed"):

        def fail(*_args: object, stage: str = stage, **_kwargs: object) -> dict[str, Any]:
            raise DispatchError(stage, f"fixture {stage}", data={"dispatch_id": "mdp-retained", "state": "failed_start", "version": 4})

        with authority_patches(), effect_patch("dispatch_managed_work", plugin, fail):
            receipt = dispatch("dispatch_managed_work", plugin)
            plugin._launch_jobs.future.result(timeout=5)
        manager = plugin._try_acquire_async_job_manager()
        job_id = receipt["data"]["job_id"]
        assert manager.get_job(job_id)["data"]["job"]["status"] == "error"
        final = manager.get_job_payload(job_id, "result")["data"]["payload"]
        assert final["error"]["code"] == stage and final["data"]["dispatch_id"] == "mdp-retained"
        assert manager.get_job_payload(job_id, "error")["data"]["payload"] == final["error"]
        assert state.rows["session_launch_reservation"] == []
    finish_fixture(plugin)
    print("PASS missing_context_shape_refusal, exact_worktree_host_first_turn_errors")


def worker_admission_failure() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    calls: list[str] = []
    with effect_patch("spawn_session", plugin, lambda *_args, **_kwargs: calls.append("host") or {}), patch.object(plugin._launch_jobs._executor, "submit", side_effect=RuntimeError("injected admission refusal")):
        refused = dispatch("spawn_session", plugin)
    assert refused["action_status"] != "completed" and calls == []
    assert state.rows["job"][0]["status"] == "error" and state.rows["session_launch_reservation"] == []
    manager = plugin._try_acquire_async_job_manager()
    assert manager.get_job_payload(state.rows["job"][0]["id"], "error")["data"]["payload"]["code"] == "launch_admission_failed"
    finish_fixture(plugin)
    print("PASS executor_admission_failure_persists_error_without_host_work")


def uncertain_first_turn() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    final = {"dispatch": {"dispatch_id": "mdp-uncertain", "state": "uptake_uncertain", "first_turn_delivered": False, "first_turn_error": "fixture unobserved"}, "attempt": {"agent_instance_id": "agi-uncertain"}}
    with authority_patches(), effect_patch("dispatch_managed_work", plugin, lambda *_args, **_kwargs: final):
        receipt = dispatch("dispatch_managed_work", plugin)
        plugin._launch_jobs.future.result(timeout=5)
    manager = plugin._try_acquire_async_job_manager()
    job_id = receipt["data"]["job_id"]
    assert manager.get_job(job_id)["data"]["job"]["status"] == "completed"
    assert manager.get_job_payload(job_id, "result")["data"]["payload"]["data"] == final
    finish_fixture(plugin)
    print("PASS completed_operation_preserves_uncertain_first_turn")


def request_and_outcome_failure() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    state.fail_write = "job_payload"
    with effect_patch("spawn_session", plugin, lambda *_args, **_kwargs: {"forbidden": True}):
        failed = dispatch("spawn_session", plugin)
    assert failed["action_status"] != "completed" and not state.rows.get("job")
    assert state.rows["session_launch_reservation"] == []
    state.fail_write = ""

    def side_effect(*_args: object, **_kwargs: object) -> dict[str, Any]:
        state.fail_write = "job_payload"
        return {"agent_instance_id": "agi-effect-completed"}

    with effect_patch("spawn_session", plugin, side_effect):
        receipt = dispatch("spawn_session", plugin)
        plugin._launch_jobs.future.result(timeout=5)
    job_id = receipt["data"]["job_id"]
    assert state.rows["job"][0]["status"] == "processing"
    owner = dict(state.rows["session_launch_reservation"][0])
    assert owner["job_id"] == job_id
    release_launch(cast(Any, state), "wrong-owner", os.getpid())
    assert state.rows["session_launch_reservation"] == [owner]
    state.fail_write = ""
    plugin._launch_jobs.stop()
    plugin._launch_jobs.start()
    other = plugin_fixture(state)
    for competitor in (plugin, other):
        busy = dispatch("spawn_session", competitor)
        assert busy["error"]["code"] == "launch_busy" and busy["data"]["job_id"] == job_id
    assert len(state.rows["job"]) == 1
    finish_fixture(plugin)
    finish_fixture(other)
    print("PASS request_failure_releases, terminal_failure_retains_owner_across_restart_and_colors")


def restart_and_delivery_failure() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    manager = plugin._try_acquire_async_job_manager()
    receipt = manager.create_job("agent_messaging_plugin", "restart_session", {"notes": "fixture restart"})
    job_id = receipt["data"]["job_id"]
    calls: list[str] = []

    def restart(_request: dict[str, object]) -> dict[str, object]:
        assert state.rows["session_launch_reservation"][0]["job_id"] == job_id
        busy = dispatch("spawn_session", plugin)
        assert busy["error"]["code"] == "launch_busy"
        run_reserved_restart(manager, cast(Any, state), job_id, lambda _request: calls.append("duplicate") or {})
        assert manager.get_job(job_id)["data"]["job"]["status"] == "processing"
        calls.append("restart")
        return {"new_agent_instance_id": "agi-restarted"}

    run_reserved_restart(manager, cast(Any, state), job_id, restart)
    run_reserved_restart(manager, cast(Any, state), job_id, lambda _request: calls.append("stale duplicate") or {})
    assert calls == ["restart"] and len(state.rows["job"]) == 1
    assert state.rows["session_launch_reservation"] == []
    reserve_launch(cast(Any, state), "existing-owner", "existing-job")
    second = manager.create_job("agent_messaging_plugin", "restart_session", {"notes": "busy restart"})["data"]["job_id"]
    before = deepcopy(state.rows)
    run_reserved_restart(manager, cast(Any, state), second, restart)
    assert calls == ["restart"] and state.rows == before
    release_launch(cast(Any, state), "existing-owner", os.getpid())
    with effect_patch("spawn_session", plugin, lambda *_args, **_kwargs: {"agent_instance_id": "agi-final"}), patch.object(manager, "_route_completion_to_role", side_effect=RuntimeError("push failed")):
        plugin._try_acquire_async_job_manager = lambda: manager
        receipt = dispatch("spawn_session", plugin)
        plugin._launch_jobs.future.result(timeout=5)
    assert manager.get_job(receipt["data"]["job_id"])["data"]["job"]["status"] == "completed"
    assert state.rows["session_launch_reservation"] == []
    plugin._launch_jobs.stop()
    assert dispatch("spawn_session", plugin)["action_status"] != "completed"
    finish_fixture(plugin)
    print("PASS restart_uses_existing_job, restart_busy_keeps_job_untouched, delivery_failure_releases_committed_owner, stopped_admission")


def duplicate_restart_request_failure(failure: str) -> None:
    """Drive both colors through the real entry point, keeping the owner active."""
    state = JobState()
    owner, duplicate = plugin_fixture(state), plugin_fixture(state)
    manager = owner._try_acquire_async_job_manager()
    duplicate_manager = duplicate._try_acquire_async_job_manager()
    request: dict[str, object] = {"notes": "two-color restart request fixture"}
    job_id = manager.create_job("agent_messaging_plugin", "restart_session", request)["data"]["job_id"]
    stale_selection = deepcopy(state.rows["job"][0])
    result: dict[str, object] = {"new_agent_instance_id": "agi-rightful-restart", "role_name": "Fixture-Worker"}
    with blocked_restart_owner(owner, manager, state, stale_selection, request, result) as effects, patch.object(duplicate, "_run_restart_session_operation", side_effect=AssertionError("duplicate_restart_effect")):
        try:
            before = deepcopy(state.rows)
            with (
                patch.object(duplicate_manager, "get_job_payload", side_effect=lambda *_args: restart_request_failure(failure)) as request_read,
                patch.object(duplicate_manager, "update_job", wraps=duplicate_manager.update_job) as job_write,
                patch.object(state, "delete_records", wraps=state.delete_records) as reservation_release,
            ):
                duplicate._process_choreography_job(stale_selection, duplicate_manager, state)
                check = "request_read_failure" if failure == "transient" else f"{failure}_request"
                assert state.rows["job"][0]["status"] == "processing", f"duplicate_restart_{check}_must_not_terminalize_active_owner"
                assert state.rows == before, f"duplicate_restart_{failure}_preserves_owner_payload_and_reservation"
                request_read.assert_not_called()
                job_write.assert_not_called()
                reservation_release.assert_not_called()
        finally:
            finish_fixture(duplicate)
    assert effects == ["rightful"] and len(state.rows["job"]) == 1
    assert manager.get_job(job_id)["data"]["job"]["status"] == "completed"
    assert manager.get_job_payload(job_id, "result")["data"]["payload"] == result
    assert state.rows["session_launch_reservation"] == []
    assert terminal_statuses(state) == ["completed"]
    before = deepcopy(state.rows)
    with patch.object(duplicate_manager, "get_job_payload", side_effect=lambda *_args: restart_request_failure(failure)) as request_read, patch.object(duplicate_manager, "update_job", wraps=duplicate_manager.update_job) as job_write:
        duplicate._process_choreography_job(stale_selection, duplicate_manager, state)
        request_read.assert_not_called()
        job_write.assert_not_called()
    assert state.rows == before and effects == ["rightful"]
    print(f"PASS duplicate_restart_{failure}_preserves_active_owner_and_exactly_one_completion")


def restart_busy_handoff_preserves_new_owner() -> None:
    observed = restart_busy_handoff()
    assert observed["status_after_rejection"] == "processing", "stale_launch_busy_outcome_must_not_terminalize_new_restart_owner"
    assert observed["owner_unchanged"], "rejected_restart_preserves_job_payload_and_reservation"
    assert observed["losing_writes_and_releases"] == (0, 0)
    assert observed["effects"] == ["rightful"]
    completed = {"status": "completed", "outcomes": ["completed"], "result": observed["expected_result"]}
    assert observed["final"] == completed
    assert observed["releases"] == [completed], "restart_commits_once_before_owner_release"
    assert observed["reservations"] == []
    print("PASS stale_launch_busy_outcome_must_not_terminalize_new_restart_owner")


def _inject_native_refusal(case: str, register: Any, state: JobState, raw: dict[str, Any]) -> None:
    from dataclasses import replace

    from _register_unit_double import FIXTURE_UNIT_ID

    from agent_messaging_plugin.register_unit_client import RegisterUnitError

    if case == "missing":
        register.units.clear()
    elif case == "retired":
        register.units[FIXTURE_UNIT_ID] = replace(register.units[FIXTURE_UNIT_ID], state="retired")
    elif case == "repository":
        register.units[FIXTURE_UNIT_ID] = replace(register.units[FIXTURE_UNIT_ID], repository_id="rep_other")
    elif case == "caller":
        state.rows["role_binding"][0]["agent_instance_id"] = "agi_other"
    elif case == "revision":
        raw["expected_revision_id"] = "drv_stale"
    elif case == "outage":
        register.show_unit = lambda _unit: (_ for _ in ()).throw(RegisterUnitError("unit_register_unavailable", "fixture outage"))


def native_refusals_have_no_launch_rows() -> None:

    cases = {
        "missing": "unit_not_found",
        "retired": "unit_not_dispatchable",
        "repository": "unit_repository_mismatch",
        "caller": "coordinator_authority_denied",
        "revision": "unit_revision_stale",
        "outage": "unit_register_unavailable",
    }
    for case, expected in cases.items():
        state = JobState()
        plugin = plugin_fixture(state)
        register = plugin._register_unit_client()
        raw = parameters("dispatch_managed_work")
        _inject_native_refusal(case, register, state, raw)
        before = deepcopy(state.rows)
        with patch.object(plugin._launch_jobs, "submit", side_effect=AssertionError("invalid Unit reached job creation")):
            result = dispatch("dispatch_managed_work", plugin, raw)
        assert result["error"]["code"] == expected, (case, result)
        assert state.rows == before, (case, "invalid request wrote ledger state")
        finish_fixture(plugin)
    print("PASS native_refusals_before_job_dispatch_or_host_creation")


def _exited_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def orphan_owner_released_on_next_launch() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    manager = plugin._try_acquire_async_job_manager()
    job_id = _owner_job(manager, "processing")
    orphan = {"resource_key": "session_launch", "operation_id": "orphan-op", "job_id": job_id}
    state.rows["session_launch_reservation"] = [{**orphan, "owner_pid": _exited_pid(), "owner_started": "fixture start"}]
    receipt, advisories = _launch_with_advisories(plugin)
    assert receipt["action_status"] == "completed", "orphan_reservation_must_not_block_launch"
    released = f"Launch reservation released: operation=orphan-op job={job_id} reason=dead_owner"
    assert released in advisories, "advisory_names_operation_and_job"
    assert state.rows["session_launch_reservation"] == []
    reserve_launch(cast(Any, state), "live-op", _owner_job(manager, "processing"))
    busy, _ = _launch_with_advisories(plugin)
    assert _error_code(busy) == "launch_busy", "live_owner_in_orphan_routine_must_refuse"
    finish_fixture(plugin)
    print("PASS orphan_owner_released_on_next_launch")


def terminal_owner_released_with_advisory() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    manager = plugin._try_acquire_async_job_manager()
    job_id = _owner_job(manager, "completed")
    reserve_launch(cast(Any, state), "terminal-op", job_id)
    receipt, advisories = _launch_with_advisories(plugin)
    assert receipt["action_status"] == "completed", "terminal_owner_must_release_with_advisory"
    assert f"Launch reservation released: operation=terminal-op job={job_id} reason=terminal_job" in advisories
    assert state.rows["session_launch_reservation"] == []
    finish_fixture(plugin)
    print("PASS terminal_owner_released_with_advisory")


def live_owner_still_refuses_launch() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    job_id = _owner_job(plugin._try_acquire_async_job_manager(), "processing")
    reserve_launch(cast(Any, state), "live-op", job_id)
    before = deepcopy(state.rows["session_launch_reservation"])
    busy, advisories = _launch_with_advisories(plugin)
    assert _error_code(busy) == "launch_busy" and busy["data"]["job_id"] == job_id, "live_owner_must_refuse"
    assert state.rows["session_launch_reservation"] == before and advisories == []
    finish_fixture(plugin)
    print("PASS live_owner_still_refuses_launch")


def legacy_null_pid_not_released() -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    job_id = _owner_job(plugin._try_acquire_async_job_manager(), "processing")
    legacy = {"id": "slr-legacy", "resource_key": "session_launch", "operation_id": "legacy-op", "job_id": job_id}
    state.rows["session_launch_reservation"] = [dict(legacy)]
    busy, advisories = _launch_with_advisories(plugin)
    assert _error_code(busy) == "launch_busy", "legacy_null_pid_owner_must_refuse"
    assert state.rows["session_launch_reservation"] == [legacy]
    kept = f"Launch reservation kept: operation=legacy-op job={job_id} reason=owner_unproven"
    assert advisories == [kept], "legacy_owner_kept_with_advisory"
    finish_fixture(plugin)
    print("PASS legacy_null_pid_not_released")


def release_refuses_reused_owner_pid() -> None:
    state = JobState()
    reserve_launch(cast(Any, state), "owner-op", "owner-job")
    owner = deepcopy(state.rows["session_launch_reservation"])
    release_launch(cast(Any, state), "owner-op", _exited_pid())
    assert state.rows["session_launch_reservation"] == owner, "stale_owner_pid_release_must_not_delete"
    release_launch(cast(Any, state), "owner-op", os.getpid())
    assert state.rows["session_launch_reservation"] == []
    print("PASS release_refuses_reused_owner_pid")


def main() -> None:
    native_refusals_have_no_launch_rows()
    refusal_and_stage_results()
    worker_admission_failure()
    uncertain_first_turn()
    request_and_outcome_failure()
    restart_and_delivery_failure()
    restart_busy_handoff_preserves_new_owner()
    orphan_owner_released_on_next_launch()
    terminal_owner_released_with_advisory()
    live_owner_still_refuses_launch()
    legacy_null_pid_not_released()
    release_refuses_reused_owner_pid()
    null_pid_terminal_owner_released()
    own_start_token_read_once()
    conflict_probes_owner_once()
    for failure in ("transient", "malformed", "exception"):
        duplicate_restart_request_failure(failure)
        owned_restart_request_failure(failure)


if __name__ == "__main__":
    main()
