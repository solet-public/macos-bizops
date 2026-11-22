#!/usr/bin/env python3
"""No-database contract smoke for stored peer sends that cannot wake a watcher.

A stored no-wake send stays ``completed``; its payload gains ``send_status`` and
``sender_notice``. The CLI cases run through the real executor, poller failure
path and ``process_result``; only database reads and writes are replaced.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT = REPO_ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402

import agent_messaging_plugin.mcp_streamable.dispatch as stream_dispatch  # noqa: E402
import agent_messaging_plugin.peer_dispatch as peer_dispatch  # noqa: E402
import agent_messaging_plugin.plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.delivery_drive import DriveOnDeliveryOutcome  # noqa: E402
from agent_messaging_plugin.local_cli import cli as cli_module  # noqa: E402
from agent_messaging_plugin.mcp_streamable.dispatch import DispatchContext  # noqa: E402
from agent_messaging_plugin.mcp_streamable.session import StreamableSession  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import (  # noqa: E402
    PeerSendOutcome,
    RoleSendOutcome,
)
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.platform_surface import PlatformSurface  # noqa: E402
from agent_messaging_plugin.role_binding_store import ResolvedRole  # noqa: E402

pass_count = 0
failed_checks: list[str] = []
MESSAGE_ID = "arm-no-wake-smoke"
NOTICE = f"The message is already stored as {MESSAGE_ID}; no turn was scheduled, so the recipient will not be woken. Do NOT resend it; nudge the recipient's pane or tell the operator if it matters."
ROLE = ResolvedRole(
    name="Target",
    agent_id="codex",
    agent_instance_id="agi-target",
    agent_session_id="ases-target",
    session_label="Target",
)
SENDER = SimpleNamespace(
    bridge_id="agc-sender",
    agent_id="codex",
    agent_instance_id="agi-sender",
    session_label="Sender",
    reply_to_role="",
)


def _check(condition: object, name: str) -> None:
    global pass_count
    if condition:
        pass_count += 1
        print(f"PASS {name}")
    else:
        failed_checks.append(name)
        print(f"FAIL {name}")


def _role_outcome(
    drive: DriveOnDeliveryOutcome,
    delivery: str = "queued_watcher",
) -> RoleSendOutcome:
    return RoleSendOutcome(
        thread_id="role:Target",
        message_id=MESSAGE_ID,
        role=ROLE,
        delivery=delivery,
        drive_on_delivery=drive,
        delivered_to_bridge_id="agc-target",
    )


def _peer_outcome(drive: DriveOnDeliveryOutcome) -> PeerSendOutcome:
    return PeerSendOutcome(
        thread_id="thread-1",
        message_id=MESSAGE_ID,
        cursor=1,
        delivered_to_agent_id="codex",
        delivered_to_agent_instance_id="agi-target",
        from_agent_id="codex",
        from_agent_instance_id="agi-sender",
        delivery="queued_watcher",
        drive_on_delivery=drive,
        delivered_to_bridge_id="agc-target",
    )


def _plugin() -> Any:
    plugin = object.__new__(plugin_module.AgentMessagingPlugin)
    plugin._peer_registry = object()
    plugin._bridge_manager = object()
    plugin._get_state_service = lambda: object()
    plugin._require_service = lambda: object()
    return plugin


def _call_peer_send_by_name(
    drive: DriveOnDeliveryOutcome,
    delivery: str = "queued_watcher",
) -> dict[str, object]:
    plugin = _plugin()
    with (
        patch.object(plugin_module, "_retired_role_refusal", return_value=None),
        patch.object(plugin_module, "resolve_role_binding", return_value=ROLE),
        patch.object(plugin_module, "_resolve_role_send_sender", return_value=SENDER),
        patch.object(plugin_module, "dispatch_role_send", return_value=_role_outcome(drive, delivery)),
    ):
        return plugin.peer_send_by_name({"name": "Target", "content": "hello"}, {})


def _call_peer_send(drive: DriveOnDeliveryOutcome) -> dict[str, object]:
    plugin = _plugin()
    with (
        patch.object(plugin_module, "_resolve_role_send_sender", return_value=SENDER),
        patch.object(plugin_module, "sole_role_for_reply_address", return_value=""),
        patch.object(plugin_module, "dispatch_peer_send", return_value=_peer_outcome(drive)),
    ):
        return plugin.peer_send_action(
            {"peer_id": "codex", "peer_agent_instance_id": "agi-target", "content": "hello"},
            {},
        )


def _stream_context() -> tuple[StreamableSession, DispatchContext]:
    binding = BridgeBinding(
        bridge_id="agc-sender",
        agent_id="codex",
        agent_instance_id="agi-sender",
        session_label="Sender",
        parent_pid=None,
    )
    session = StreamableSession(
        mcp_session_id="mcp-session",
        bridge_id="agc-sender",
        session_id="session",
        agent_id="codex",
        agent_instance_id="agi-sender",
        session_label="Sender",
        binding=binding,
    )
    context = DispatchContext(
        bridge_manager=object.__new__(BridgeSessionManager),
        peer_registry=object.__new__(PeerRegistry),
        platform_surface=object.__new__(PlatformSurface),
        agent_messaging_service=object(),
        state_service=object(),
    )
    return session, context


def _call_streamable_peer_send(drive: DriveOnDeliveryOutcome) -> dict[str, object]:
    session, context = _stream_context()
    with patch.object(stream_dispatch, "dispatch_peer_send", return_value=_peer_outcome(drive)):
        return stream_dispatch._tool_peer_send(
            {"peer_id": "codex", "content": [{"type": "text", "text": "hello"}]},
            session=session,
            context=context,
        )


def _call_streamable_role_send(drive: DriveOnDeliveryOutcome) -> dict[str, object]:
    session, context = _stream_context()
    with (
        patch.object(stream_dispatch, "_refuse_retired_role", return_value=None),
        patch.object(stream_dispatch, "resolve_role_binding", return_value=ROLE),
        patch.object(stream_dispatch, "dispatch_role_send", return_value=_role_outcome(drive)),
    ):
        return stream_dispatch._tool_peer_send_by_name(
            {"name": "Target", "content": "hello"},
            session=session,
            context=context,
        )


def _stored_receipt(result: dict[str, object]) -> bool:
    data = result.get("data")
    return result.get("action_status") == "completed" and isinstance(data, dict) and data.get("message_id") == MESSAGE_ID and data.get("delivery") == "queued_watcher" and data.get("drive_on_delivery") == "driver_unavailable" and data.get("send_status") == "stored_no_wake" and data.get("sender_notice") == NOTICE


_SEND_KEY = "plugin::agent_messaging_plugin::peer_send_by_name"


def _real_process_envelope(
    verb_result: dict[str, object],
    dispatched: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The envelope ``solet-bridge call`` reads for a verb that returned ``verb_result``.

    Runs the REAL ``ActionProcessor.execute_action``, the REAL poller
    ``_process_action`` (including its REAL failure path, ``_mark_action_failed``)
    and the REAL ``PlatformSurface.process_result``. Replaced: the database
    reads and writes beneath them (including the completed-result write, whose
    argument is the executor's result unchanged) and the in-memory execution
    context store. Every error-handler dispatch the failure path would make is
    appended to ``dispatched``.
    """
    stored: list[dict[str, object]] = []
    failure_dispatches = dispatched if dispatched is not None else []

    class _RecordingErrorDispatcher:
        def dispatch_execution_failure(self, **kwargs: Any) -> None:
            failure_dispatches.append(kwargs)

    action = SimpleNamespace(
        id="ae-real-chain",
        process_key=_SEND_KEY,
        parameters="{}",
        flow_id="flow-real",
        flow_token_id=None,
    )
    poller = object.__new__(ActionQueuePoller)
    poller.action_processor = object.__new__(ActionProcessor)  # type: ignore[assignment]
    failed_details = (_SEND_KEY, "{}", None, None, None, "flow-real", None, None)
    with (
        patch.object(ActionProcessor, "_parse_process_key", return_value=("plugin", "agent_messaging_plugin", "peer_send_by_name")),
        patch.object(ActionProcessor, "_prepare_arguments", return_value={}),
        patch.object(ActionProcessor, "_resolve_placeholders", return_value={}),
        patch.object(ActionProcessor, "_execute_by_provider_type", return_value=verb_result),
        patch.object(ActionProcessor, "_store_result_in_context"),
        patch.object(ActionQueuePoller, "_is_terminated_flow_sibling", return_value=False),
        patch.object(ActionQueuePoller, "_prepare_action_for_execution", return_value=True),
        patch.object(ActionQueuePoller, "_resolve_io_context"),
        patch.object(ActionQueuePoller, "_requires_main_thread", return_value=True),
        patch.object(ActionQueuePoller, "_mark_action_completed", side_effect=lambda _id, result, *_a, **_k: stored.append(result)),
        patch.object(ActionQueuePoller, "_update_action_status_to_failed"),
        patch.object(ActionQueuePoller, "_retrieve_failed_action_details", return_value=failed_details),
        patch.object(ActionQueuePoller, "_store_action_result", side_effect=lambda _id, result, _key: stored.append(result)),
        patch.object(ActionQueuePoller, "_process_error_processor_template"),
        patch.object(ActionQueuePoller, "_get_error_dispatcher", return_value=_RecordingErrorDispatcher()),
    ):
        asyncio.run(poller._process_action(action))  # type: ignore[arg-type]
    surface = object.__new__(PlatformSurface)
    surface._state_service = object()  # type: ignore[attr-defined]
    status = "completed" if verb_result.get("action_status") == "completed" else "failed"
    event = {"status": status, "process_key": _SEND_KEY, "core__flows_id": "flow-real"}
    latest = {"result_data": stored[-1], "result_source": _SEND_KEY, "created_at": "now"}
    with (
        patch.object(PlatformSurface, "_read_action_event", return_value=event),
        patch.object(PlatformSurface, "_read_latest_action_result", return_value=latest),
    ):
        return surface.process_result(action.id)


def _not_delivered_yet(data: object) -> bool:
    """A receipt the drive has not reached yet: its status, its outcome read and the notice that says so."""
    return (
        isinstance(data, dict)
        and data.get("send_status") == "stored_not_delivered_yet"
        and data.get("outcome_read") == {"process": "plugin::agent_messaging_plugin::peer_send_outcome", "parameters": {"message_id": MESSAGE_ID}}
        and all(text in str(data.get("sender_notice")) for text in ("NOT DELIVERED YET", "peer_send_outcome", "Do NOT resend"))
    )


def test_peer_send_by_name_action() -> None:
    result = _call_peer_send_by_name("driver_unavailable")
    _check(_stored_receipt(result), "peer_send_by_name stored receipt, status stays completed, no-resend notice")
    scheduled = _call_peer_send_by_name("driver_scheduled")
    scheduled_data = scheduled.get("data")
    _check(scheduled.get("action_status") == "completed" and _not_delivered_yet(scheduled_data), "WAKE_SCHEDULED_NOT_CONFIRMED completed scheduled role send")


def test_scheduled_receipts_are_unconfirmed() -> None:
    for name, invoke in (("direct", _call_peer_send), ("role", _call_peer_send_by_name)):
        for drive in ("driver_scheduled", "driver_pending"):
            result = invoke(drive)
            data = result.get("data")
            _check(
                result.get("action_status") == "completed" and isinstance(data, dict) and data.get("message_id") == MESSAGE_ID and _not_delivered_yet(data),
                f"WAKE_SCHEDULED_NOT_CONFIRMED {name} {drive}",
            )


def test_peer_send_action() -> None:
    result = _call_peer_send("driver_unavailable")
    _check(_stored_receipt(result), "peer_send stored receipt, status stays completed, no-resend notice")


def test_one_name_for_the_send_verb() -> None:
    """The CLI process, its KB article and the MCP tool share one name, with no alias left."""
    metadata = getattr(plugin_module.AgentMessagingPlugin.peer_send_action, "_platform_process_metadata", None)
    _check(getattr(metadata, "name", None) == "peer_send", "the verb's process name is peer_send, the MCP tool's name")
    _check(not hasattr(plugin_module.AgentMessagingPlugin, "send_peer_message"), "the old verb name is gone, no alias")
    article = json.loads((REPO_ROOT / "plugins" / "agent_messaging_plugin" / "knowledge_base" / "processes" / "peer_send.json").read_text())
    _check(article["process_key"] == "plugin::agent_messaging_plugin::peer_send", "the KB article is keyed by the one name")
    _check(callable(stream_dispatch._tool_peer_send), "the MCP peer_send tool entry is wired")  # noqa: SLF001


def test_streamable_send_tools() -> None:
    peer = _call_streamable_peer_send("driver_unavailable")
    role = _call_streamable_role_send("driver_unavailable")
    for name, result in (("peer_send", peer), ("peer_send_by_name MCP", role)):
        _check(
            result.get("message_id") == MESSAGE_ID and result.get("delivery") == "queued_watcher" and result.get("drive_on_delivery") == "driver_unavailable" and result.get("send_status") == "stored_no_wake" and "Do NOT resend" in str(result.get("sender_notice")),
            f"{name} exposes stored receipt and no-resend notice",
        )


def _check_real_queue_keeps_send_completed(
    envelope: dict[str, Any],
    dispatched: list[dict[str, Any]],
) -> None:
    stored = envelope["result"]
    _check(
        envelope["status"] == "completed" and stored.get("success") is True and stored.get("action_status") == "completed",
        "the real queue path keeps the stored no-wake send completed",
    )
    _check(len(dispatched) == 0, "the real queue path makes NO error-handler dispatch for a stored no-wake send")


def _check_cli_output(response: Any) -> None:
    _check(response.exit_code == 0, "local CLI exits zero for stored_no_wake on the real queue path")
    printed = json.loads(response.stdout)
    receipt = printed["result"]["data"]
    _check(
        printed["status"] == "completed" and receipt.get("send_status") == "stored_no_wake" and receipt.get("sender_notice") == NOTICE and receipt.get("message_id") == MESSAGE_ID and receipt.get("delivery") == "queued_watcher" and receipt.get("drive_on_delivery") == "driver_unavailable",
        "local CLI stdout preserves all five receipt fields on the real queue path",
    )
    _check(
        response.stderr.count("WARNING:") == 1 and "Do NOT resend" in response.stderr and MESSAGE_ID in response.stderr and "\n" not in response.stderr.rstrip("\n"),
        "local CLI writes one-line no-resend warning naming the stored message_id to stderr",
    )


def test_local_cli_prints_receipt_and_warns_without_failing() -> None:
    """``solet-bridge call`` through the REAL queue path (execute_action, poller, process_result)."""
    from click.testing import CliRunner

    action_result = _call_peer_send_by_name("driver_unavailable")
    dispatched: list[dict[str, Any]] = []
    envelope = _real_process_envelope(action_result, dispatched)
    _check_real_queue_keeps_send_completed(envelope, dispatched)
    with patch.object(cli_module, "_run", side_effect=lambda callback: callback(SimpleNamespace(call_and_wait=lambda *_args, **_kwargs: envelope))):
        response = CliRunner().invoke(
            cli_module.cli,
            ["call", "plugin::agent_messaging_plugin::peer_send_by_name", "{}"],
        )
    _check_cli_output(response)


def _identity_notice_mutant(
    payload: dict[str, Any],
    *,
    delivery: str,
    drive_on_delivery: DriveOnDeliveryOutcome,
) -> dict[str, Any]:
    """Mutant: omit the stored-no-wake classification on every outcome."""
    del delivery, drive_on_delivery
    return payload


def test_no_notice_mutant_is_red_for_each_sender_path() -> None:
    from click.testing import CliRunner

    print(f"LOADED peer_dispatch={peer_dispatch.__file__}")
    print(f"LOADED plugin={plugin_module.__file__}")
    print(f"LOADED stream_dispatch={stream_dispatch.__file__}")
    print(f"LOADED local_cli={cli_module.__file__}")
    mutant_paths: tuple[tuple[str, Any], ...] = (
        ("peer_send_by_name action mapping", lambda: _call_peer_send_by_name("driver_unavailable")),
        ("peer_send action mapping", lambda: _call_peer_send("driver_unavailable")),
        ("MCP peer_send payload", lambda: _call_streamable_peer_send("driver_unavailable")),
        ("MCP peer_send_by_name payload", lambda: _call_streamable_role_send("driver_unavailable")),
    )
    for name, invoke in mutant_paths:
        with patch.object(peer_dispatch, "_add_stored_no_wake_notice", _identity_notice_mutant):
            result = invoke()
        detected = not _stored_receipt(result) if name.endswith("action mapping") else result.get("send_status") != "stored_no_wake"
        _check(detected, f"MUTANT RED {name}: its named send contract assertion fails")

    with patch.object(peer_dispatch, "_add_stored_no_wake_notice", _identity_notice_mutant):
        action_result = _call_peer_send_by_name("driver_unavailable")
    envelope = _real_process_envelope(action_result)
    with patch.object(cli_module, "_run", side_effect=lambda callback: callback(SimpleNamespace(call_and_wait=lambda *_args, **_kwargs: envelope))):
        response = CliRunner().invoke(
            cli_module.cli,
            ["call", "plugin::agent_messaging_plugin::peer_send_by_name", "{}"],
        )
    _check(
        response.exit_code != 0 or "WARNING:" not in response.stderr,
        "MUTANT RED local CLI no-wake warning: omitted send_status suppresses warning",
    )


def _stored_no_wake_without_outcome_read(result: dict[str, object]) -> bool:
    data = result.get("data")
    return result.get("action_status") == "completed" and isinstance(data, dict) and data.get("send_status") == "stored_no_wake" and "outcome_read" not in data


def test_other_drive_outcomes_and_delivery_classes() -> None:
    for drive in ("not_managed", "ineligible_state", "driver_queue_full", "driver_error"):
        result = _call_peer_send_by_name(drive)
        data = result.get("data")
        _check(
            result.get("action_status") == "completed" and isinstance(data, dict) and data.get("drive_on_delivery") == drive and data.get("send_status") == "stored_no_wake",
            f"no-drive sibling {drive} is marked stored_no_wake and stays completed",
        )
    watcher = _call_peer_send_by_name("driver_unavailable", "queued_watcher")
    _check(_stored_no_wake_without_outcome_read(watcher), "T3: queued_watcher with driver_unavailable is stored_no_wake and carries no outcome_read")
    replay = _call_peer_send_by_name("driver_unavailable", "queued_for_replay")
    replay_data = replay.get("data")
    _check(
        replay.get("action_status") == "completed" and isinstance(replay_data, dict) and replay_data.get("send_status") == "stored_no_wake",
        "M3: queued_for_replay with a no-drive outcome is stored_no_wake (dropping it from the no-wake set must fail here)",
    )
    for delivery in ("queued_wake", "queued_notification"):
        payload = _role_outcome("driver_unavailable", delivery).to_payload()
        _check("send_status" not in payload, f"{delivery} is not falsely called a non-wake")


def main() -> int:
    tests = (
        test_peer_send_by_name_action,
        test_peer_send_action,
        test_one_name_for_the_send_verb,
        test_scheduled_receipts_are_unconfirmed,
        test_streamable_send_tools,
        test_local_cli_prints_receipt_and_warns_without_failing,
        test_other_drive_outcomes_and_delivery_classes,
        test_no_notice_mutant_is_red_for_each_sender_path,
    )
    for test in tests:
        test()
    print(f"{pass_count} passed; {len(failed_checks)} failed")
    if failed_checks:
        print("Failing checks: " + ", ".join(failed_checks))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
