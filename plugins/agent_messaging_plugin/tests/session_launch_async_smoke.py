#!/usr/bin/env python3
"""Real serial poller + public launch adapters, with event-blocked effect boundaries.

No database, real register, filesystem provisioning, or real host launch.
The envelope check detects the base omission and a nested-receipt mutant.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(ROOT / "ananta" / "tests" / "core" / "actions"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from async_job_fixture_support import JobState  # noqa: E402  # pyright: ignore[reportMissingImports] - resolved at runtime by the sys.path.insert calls above; plugins/<X>/tests is not a pyright extraPath
from session_launch_fixture_support import (  # noqa: E402
    authority_patches,
    dispatch,
    effect_patch,
    finish_fixture,
    parameters,
    plugin_fixture,
    serial_pair,
)

from agent_messaging_plugin.session_launch_adapters import submit_launch  # noqa: E402

VERBS = ("spawn_session", "dispatch_managed_work", "provision_role_session", "resolve_managed_dispatch", "qualify_fleet")


ENVELOPE_KEYS = {"action_status", "data", "actions", "error", "timestamp"}


def assert_envelope(result: dict[str, Any], status: str = "completed") -> None:
    assert ENVELOPE_KEYS <= result.keys(), f"launch_envelope_missing_fields: {result}"
    assert result["action_status"] == status
    assert result["actions"] == []
    assert isinstance(result["timestamp"], str) and result["timestamp"]
    assert (result["error"] is None) == (status == "completed")


def prove_submission_envelope() -> None:
    """Fake manager receipt pins the public envelope and keeps arbitrary job data flat."""
    data: dict[str, object] = {"job_id": "job-fixture", "status": "queued", "progress_percent": 0, "extra_receipt_field": {"retained": True}}
    receipt: dict[str, object] = {"action_status": "completed", "data": data}

    def create_job() -> dict[str, object]:
        return receipt

    manager = SimpleNamespace(create_job=create_job)

    def submit(**kwargs: object) -> dict[str, object]:
        assert kwargs["manager"] is manager
        return manager.create_job()

    bound = plugin_fixture(JobState())
    plugin = SimpleNamespace(_get_state_service=bound._get_state_service, _dispatch_actor_from_state=bound._dispatch_actor_from_state, _register_unit_client=bound._register_unit_client, _try_acquire_async_job_manager=lambda: manager, _launch_jobs=SimpleNamespace(submit=submit), qualify_fleet=lambda *_args: None)
    context = {"session_id": "session-fixture", "flow_id": "flow-fixture"}
    result = submit_launch(cast(Any, plugin), "qualify_fleet", parameters("qualify_fleet"), context)
    assert_envelope(result)
    assert result["data"] == data and "data" not in result["data"], "launch_receipt_data_shape"
    assert receipt == {"action_status": "completed", "data": data}, "receipt must remain unchanged"
    invalid_receipts: tuple[dict[str, object], ...] = (
        {"action_status": "error", "data": data},
        {"action_status": "completed", "data": []},
    )
    for invalid in invalid_receipts:
        receipt = invalid
        failed = submit_launch(cast(Any, plugin), "qualify_fleet", parameters("qualify_fleet"), context)
        assert_envelope(failed, "failed")
        assert failed["error"]["code"] == "launch_submission_failed"
    finish_fixture(bound)
    print("PASS launch_submission_envelope: complete keys, flat full receipt data, loud invalid receipt")


def _seed_for_verb(verb: str, plugin: Any, state: JobState) -> None:
    if verb == "resolve_managed_dispatch":
        from session_launch_fixture_support import seed_retry_dispatch

        seed_retry_dispatch(plugin, state)


async def prove_progress(verb: str) -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    other_color = plugin_fixture(state)
    _seed_for_verb(verb, plugin, state)
    entered, release, cheap = threading.Event(), threading.Event(), threading.Event()
    calls: list[str] = []
    results: list[dict[str, Any]] = []
    final = {"agent_instance_id": "agi-fixture-child", "first_turn_delivered": True, "attempt_number": 2}

    def operation(*_args: object, **_kwargs: object) -> dict[str, Any]:
        calls.append(verb)
        entered.set()
        assert release.wait(5), "fixture release was never set"
        return final

    with authority_patches(), effect_patch(verb, plugin, operation):
        task = asyncio.create_task(serial_pair(verb, plugin, cheap, results))
        try:
            assert await asyncio.to_thread(entered.wait, 2), f"{verb}: full operation entered"
            assert await asyncio.to_thread(cheap.wait, 1), f"{verb}: cheap_trailing_read_before_release"
            assert not release.is_set() and calls == [verb]
            assert_envelope(results[0])
            receipt = results[0]["data"]
            job_id = receipt["job_id"]
            assert receipt["status"] == "queued" and "agent_instance_id" not in receipt
            assert_busy(verb, plugin, other_color, job_id)
            assert len(state.rows["job"]) == 1 and calls == [verb]
        finally:
            release.set()
            await task
            finish_fixture(plugin)
            finish_fixture(other_color)
        manager = plugin._try_acquire_async_job_manager()
        assert manager.get_job(job_id)["data"]["job"]["status"] == "completed"
        payload = manager.get_job_payload(job_id, "result")["data"]["payload"]
        assert payload["data"] == final, payload
        assert state.rows["session_launch_reservation"] == []
        assert getattr(plugin, verb)(parameters(verb), {"session_id": "s", "flow_id": "f"})["data"] == final
    print(f"PASS {verb}: cheap_trailing_read_before_release, cross_color_busy, exact_final_payload")


def assert_busy(verb: str, plugin: Any, other_color: Any, job_id: str) -> None:
    assert getattr(plugin, verb + "_action")._platform_process_metadata.is_async is True
    for competitor in (plugin, other_color):
        busy = dispatch(verb, competitor)
        assert_envelope(busy, "failed")
        assert busy["error"]["code"] == "launch_busy", busy
        assert busy["data"]["job_id"] == job_id


def prove_provider_env_is_not_persisted() -> None:
    """A spawn_session provider overlay reaches the in-process operation but is
    absent from the persisted job request_data (credentials must not land in
    the job row)."""
    import json
    from unittest.mock import patch

    overlay_value = "overlay-fixture-value-7431"
    persisted: dict[str, str] = {}
    seen: dict[str, Any] = {}
    receipt: dict[str, object] = {"action_status": "completed", "data": {"job_id": "job-provider-env", "status": "queued"}}

    def submit(**kwargs: object) -> dict[str, object]:
        persisted["request"] = json.dumps(kwargs["parameters"], sort_keys=True)
        operation = cast(Any, kwargs["operation"])
        operation()
        return receipt

    def spawn_session(params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        del state
        seen["params"] = params
        return {"action_status": "completed", "data": {}, "actions": [], "error": None, "timestamp": "2026-11-03T00:00:00Z"}

    plugin = SimpleNamespace(
        _get_state_service=lambda: object(),
        _try_acquire_async_job_manager=lambda: object(),
        _launch_jobs=SimpleNamespace(submit=submit),
        spawn_session=spawn_session,
    )
    params = {**parameters("spawn_session"), "provider_env": {"PROVIDER_FIXTURE_KEY": overlay_value}}
    context = {"session_id": "session-fixture", "flow_id": "flow-fixture"}
    with patch("agent_messaging_plugin.session_launch_adapters._preflight"):
        result = submit_launch(cast(Any, plugin), "spawn_session", params, context)
    assert_envelope(result)
    assert overlay_value not in persisted["request"], "provider overlay value must not be written to the persisted job payload"
    assert "PROVIDER_FIXTURE_KEY" not in persisted["request"], "provider overlay key must not be written to the persisted job payload"
    assert seen["params"].get("provider_env") == {"PROVIDER_FIXTURE_KEY": overlay_value}, "the in-process operation still receives the overlay"
    print("PASS provider_env_not_persisted: overlay absent from request_data, present in-process")


def main() -> None:
    prove_submission_envelope()
    prove_provider_env_is_not_persisted()
    for verb in VERBS:
        asyncio.run(prove_progress(verb))


if __name__ == "__main__":
    main()
